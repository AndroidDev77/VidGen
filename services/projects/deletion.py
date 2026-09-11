"""Delete a project and everything the pipeline recorded for it.

Most tables hang off ``projects`` with ``ON DELETE CASCADE``, but the rows that
carry provenance -- generation runs, render jobs, publications -- point at
assets, scripts and runs with ``ON DELETE RESTRICT``, so the database refuses a
plain ``DELETE FROM projects``. Hand-listing the ~90 tables in a safe order
would go stale the first time a stage adds one, so this module reads the live
``Base.metadata`` graph instead: it walks foreign keys outward from the project
row to find every dependent row, orders the tables children-first, and deletes
them in that order.

Two properties keep the walk from reaching past the project it was asked for:

* a table that carries its own ``project_id`` is scoped by that column alone,
  so a sibling project's row is never pulled in through a shared asset;
* a row outside the project that references one of its assets still blocks the
  delete with an ``IntegrityError``, which is the honest answer -- the data is
  genuinely still in use.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Column, Table, delete, or_, select, update
from sqlalchemy.orm import Session

import vidgen.db  # noqa: F401  # ensures every model module is registered on Base.metadata
from vidgen.db.base import Base

PROJECT_TABLE = "projects"

# Statements are emitted one chunk of identifiers at a time so neither the
# bound-parameter limit nor the statement cache is stressed by a large project.
_CHUNK_SIZE = 500


class ProjectDeletionError(RuntimeError):
    """The foreign-key graph could not be ordered for deletion."""


def delete_project_rows(session: Session, project_id: UUID) -> list[str]:
    """Delete the project and every row that depends on it.

    Returns the blob storage keys of the deleted assets so the caller can
    remove them once the transaction commits. The caller owns the transaction:
    nothing here commits, so a failure rolls the whole project back intact.
    """
    scope = _collect_scope(session, project_id)
    storage_keys = _asset_storage_keys(session, scope)
    order, nullable_edges = _deletion_plan(scope)
    # Cycles (and self-references) in the graph are broken by clearing the
    # nullable side first: a shot pointing at its selected image, a repair run
    # at its chosen attempt.
    for table, column in nullable_edges:
        for chunk in _chunks(scope[table.name]):
            session.execute(
                update(table).where(_primary_key(table).in_(chunk)).values({column.name: None})
            )
    for table in order:
        _delete_rows(session, table, scope)
    # The ORM identity map still holds the rows these statements removed; drop
    # it so nothing stale is flushed back on commit.
    session.expunge_all()
    return storage_keys


def unreferenced_storage_keys(session: Session, storage_keys: list[str]) -> list[str]:
    """Narrow ``storage_keys`` to the blobs no surviving asset still points at.

    Blob storage is content-addressed, so two projects that produced identical
    bytes share one key. Deleting a project must not pull the blob out from
    under the project that is still using it. Call this once the deleting
    transaction has committed: it reads the asset rows that survived it.
    """
    assets = Base.metadata.tables["assets"]
    candidates = set(storage_keys)
    still_used: set[str] = set()
    for chunk in _chunks(candidates):
        still_used.update(
            session.scalars(
                select(assets.c.storage_key).where(assets.c.storage_key.in_(chunk))
            ).all()
        )
    return sorted(candidates - still_used)


def _collect_scope(session: Session, project_id: UUID) -> dict[str, set[Any]]:
    """Map table name to the primary keys of that table's rows for the project.

    The walk starts at the project row and follows foreign keys outward, one
    frontier at a time, so each row is discovered once no matter how many paths
    lead to it.
    """
    children = _children_index()
    scope: dict[str, set[Any]] = {PROJECT_TABLE: {project_id}}
    frontier: dict[str, set[Any]] = {PROJECT_TABLE: {project_id}}
    while frontier:
        discovered: dict[str, set[Any]] = {}
        for parent_name, parent_ids in frontier.items():
            for table, column in children.get(parent_name, ()):
                # A table with its own ``project_id`` is reached through that
                # column only: following a shared asset into it would drag a
                # sibling project's rows along.
                if parent_name != PROJECT_TABLE and _project_column(table) is not None:
                    continue
                key = _primary_key_or_none(table)
                if key is None:
                    # Link tables keyed by their own foreign keys own no
                    # children; they are deleted from their parents' scope.
                    continue
                found = _select_ids(session, key, column, parent_ids)
                new = found - scope.get(table.name, set())
                if new:
                    scope.setdefault(table.name, set()).update(new)
                    discovered.setdefault(table.name, set()).update(new)
        frontier = discovered
    for table in Base.metadata.tables.values():
        if _primary_key_or_none(table) is None and _references_scope(table, scope):
            scope.setdefault(table.name, set())
    return scope


def _deletion_plan(
    scope: dict[str, set[Any]],
) -> tuple[list[Table], list[tuple[Table, Column[Any]]]]:
    """Order the in-scope tables children-first, breaking cycles as needed.

    Returns the deletion order together with the nullable foreign-key columns
    that have to be cleared first for that order to exist at all.
    """
    tables = {name: Base.metadata.tables[name] for name in scope}
    edges: list[tuple[str, str, Column[Any]]] = []
    cleared: list[tuple[Table, Column[Any]]] = []
    for name, table in tables.items():
        for column, parent in _foreign_keys(table):
            if parent.name not in tables:
                continue
            if parent.name == name:
                # A self-reference is always a cycle of one.
                if not column.nullable:
                    raise ProjectDeletionError(
                        f"{name}.{column.name} references its own table and cannot be cleared"
                    )
                cleared.append((table, column))
                continue
            edges.append((name, parent.name, column))

    order: list[Table] = []
    remaining = set(tables)
    while remaining:
        referenced = {parent for child, parent, _ in edges if child in remaining}
        ready = sorted(remaining - referenced)
        if not ready:
            edges, broken = _break_cycle(tables, edges, remaining)
            cleared.append(broken)
            continue
        for name in ready:
            order.append(tables[name])
        remaining -= set(ready)
        edges = [edge for edge in edges if edge[0] in remaining]
    return order, cleared


def _break_cycle(
    tables: dict[str, Table],
    edges: list[tuple[str, str, Column[Any]]],
    remaining: set[str],
) -> tuple[list[tuple[str, str, Column[Any]]], tuple[Table, Column[Any]]]:
    """Drop one nullable edge from a cycle so the ordering can make progress."""
    cyclic = _cyclic_core(edges, remaining)
    for index, (child, parent, column) in enumerate(edges):
        if child in cyclic and parent in cyclic and column.nullable:
            return edges[:index] + edges[index + 1 :], (tables[child], column)
    raise ProjectDeletionError(
        "foreign-key cycle with no nullable edge among " + ", ".join(sorted(cyclic))
    )


def _cyclic_core(edges: list[tuple[str, str, Column[Any]]], remaining: set[str]) -> set[str]:
    """Narrow ``remaining`` to the tables that actually sit on a cycle.

    A table only belongs to a cycle if it both references and is referenced by
    another table still in the set, so peeling off the tables that fail either
    test until nothing moves leaves the cycles behind.
    """
    core = set(remaining)
    while True:
        referencing = {child for child, parent, _ in edges if child in core and parent in core}
        referenced = {parent for child, parent, _ in edges if child in core and parent in core}
        survivors = referencing & referenced
        if survivors == core:
            return core
        core = survivors


def _delete_rows(session: Session, table: Table, scope: dict[str, set[Any]]) -> None:
    key = _primary_key_or_none(table)
    if key is None:
        clause = _scope_clause(table, scope)
        if clause is not None:
            session.execute(delete(table).where(clause))
        return
    for chunk in _chunks(scope[table.name]):
        session.execute(delete(table).where(key.in_(chunk)))


def _asset_storage_keys(session: Session, scope: dict[str, set[Any]]) -> list[str]:
    assets = Base.metadata.tables["assets"]
    keys: list[str] = []
    for chunk in _chunks(scope.get("assets", set())):
        keys.extend(
            session.scalars(select(assets.c.storage_key).where(assets.c.id.in_(chunk))).all()
        )
    return keys


def _children_index() -> dict[str, list[tuple[Table, Column[Any]]]]:
    """Reverse the foreign-key graph: parent table name -> (child table, column)."""
    index: dict[str, list[tuple[Table, Column[Any]]]] = {}
    for table in Base.metadata.tables.values():
        for column, parent in _foreign_keys(table):
            index.setdefault(parent.name, []).append((table, column))
    return index


def _foreign_keys(table: Table) -> Iterator[tuple[Column[Any], Table]]:
    """Single-column foreign keys of ``table`` as (column, referenced table)."""
    for constraint in table.foreign_key_constraints:
        if len(constraint.elements) != 1:
            raise ProjectDeletionError(
                f"{table.name} has a composite foreign key, which this walk cannot follow"
            )
        element = constraint.elements[0]
        yield element.parent, element.column.table


def _project_column(table: Table) -> Column[Any] | None:
    for column, parent in _foreign_keys(table):
        if parent.name == PROJECT_TABLE:
            return column
    return None


def _primary_key_or_none(table: Table) -> Column[Any] | None:
    columns = list(table.primary_key.columns)
    return columns[0] if len(columns) == 1 else None


def _primary_key(table: Table) -> Column[Any]:
    key = _primary_key_or_none(table)
    if key is None:
        raise ProjectDeletionError(f"{table.name} has no single-column primary key")
    return key


def _references_scope(table: Table, scope: dict[str, set[Any]]) -> bool:
    return any(parent.name in scope for _, parent in _foreign_keys(table))


def _scope_clause(table: Table, scope: dict[str, set[Any]]) -> Any:
    clauses = [
        column.in_(chunk)
        for column, parent in _foreign_keys(table)
        for chunk in _chunks(scope.get(parent.name, set()))
    ]
    if not clauses:
        return None
    return or_(*clauses)


def _select_ids(
    session: Session, key: Column[Any], column: Column[Any], parent_ids: set[Any]
) -> set[Any]:
    found: set[Any] = set()
    for chunk in _chunks(parent_ids):
        found.update(session.scalars(select(key).where(column.in_(chunk))).all())
    return found


def _chunks(values: set[Any]) -> Iterator[Sequence[Any]]:
    ordered = list(values)
    for start in range(0, len(ordered), _CHUNK_SIZE):
        yield ordered[start : start + _CHUNK_SIZE]
