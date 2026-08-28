"""Monotonic row versions for optimistic concurrency.

The version of a resource lives in ``resource_versions`` rather than on each
domain table, so T18 adds concurrency control without reshaping the T05-T17
schema. Comparisons and increments happen inside the caller's transaction: the
service never trusts a version supplied by frontend state.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vidgen.db.review_models import ResourceVersion
from vidgen.review.errors import precondition_required, version_conflict


def parse_if_match(header: str | None) -> int | None:
    """Return the integer row version in an ``If-Match`` header, if present.

    Accepts both the bare integer and the quoted entity-tag spelling.
    """
    if header is None:
        return None
    candidate = header.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    candidate = candidate.strip('"')
    if not candidate.isdigit():
        return None
    return int(candidate)


class RowVersionService:
    """Read, require, and increment resource row versions transactionally."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # The version each resource was observed at by ``require`` in this
        # request, so ``bump`` can make its write conditional on it.
        self._observed: dict[tuple[str, UUID], int] = {}

    def _row(self, resource_type: str, resource_id: UUID) -> ResourceVersion | None:
        return self._session.scalar(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == resource_type,
                ResourceVersion.resource_id == resource_id,
            )
        )

    def current(self, project_id: UUID, resource_type: str, resource_id: UUID) -> int:
        """Return the current version, materialising version 1 on first read.

        Two concurrent reads of the same resource race to insert the first row;
        the unique constraint decides, and the loser re-reads the winner's row
        rather than failing an ordinary GET.
        """
        row = self._row(resource_type, resource_id)
        if row is not None:
            return row.version
        candidate = ResourceVersion(
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            version=1,
        )
        self._session.add(candidate)
        try:
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError:
            self._session.expunge(candidate)
            existing = self._row(resource_type, resource_id)
            if existing is None:  # pragma: no cover - the constraint guarantees a row
                raise
            return existing.version
        return candidate.version

    def require(
        self,
        project_id: UUID,
        resource_type: str,
        resource_id: UUID,
        if_match: str | None,
        *,
        label: str | None = None,
    ) -> int:
        """Enforce ``If-Match`` against the stored version and return it."""
        name = label or resource_type.replace("_", " ")
        current = self.current(project_id, resource_type, resource_id)
        expected = parse_if_match(if_match)
        if expected is None:
            raise precondition_required(name, current)
        if expected != current:
            raise version_conflict(name, current)
        self._observed[(resource_type, resource_id)] = current
        return current

    def bump(
        self,
        project_id: UUID,
        resource_type: str,
        resource_id: UUID,
        *,
        expected: int | None = None,
    ) -> int:
        """Increment and return the new version inside the caller's transaction.

        The increment is a compare-and-swap against the version this request
        already observed, so it cannot be a check-then-write: reading the row,
        comparing in Python and then updating by primary key would let two
        concurrent editors both satisfy the same ``If-Match`` and both apply
        their change. The version therefore appears in the ``WHERE`` clause, and
        a write that matches no row loses the race and raises a conflict rather
        than overwriting the winner.
        """
        name = resource_type.replace("_", " ")
        if expected is None:
            expected = self._observed.get((resource_type, resource_id))
        # Materialise version 1 for a resource nothing has versioned yet, and
        # fall back to its current version when no precondition was checked.
        current = self.current(project_id, resource_type, resource_id)
        if expected is None:
            expected = current
        row = self._row(resource_type, resource_id)
        assert row is not None
        result: CursorResult[Any] = self._session.execute(  # type: ignore[assignment]
            update(ResourceVersion)
            .where(
                ResourceVersion.resource_type == resource_type,
                ResourceVersion.resource_id == resource_id,
                ResourceVersion.version == expected,
            )
            .values(version=expected + 1)
            .execution_options(synchronize_session=False)
        )
        self._session.expire(row)
        if result.rowcount != 1:
            raise version_conflict(name, self.current(project_id, resource_type, resource_id))
        self._observed[(resource_type, resource_id)] = expected + 1
        return expected + 1
