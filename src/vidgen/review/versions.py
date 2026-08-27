"""Monotonic row versions for optimistic concurrency.

The version of a resource lives in ``resource_versions`` rather than on each
domain table, so T18 adds concurrency control without reshaping the T05-T17
schema. Comparisons and increments happen inside the caller's transaction: the
service never trusts a version supplied by frontend state.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
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
        return current

    def bump(self, project_id: UUID, resource_type: str, resource_id: UUID) -> int:
        """Increment and return the new version inside the caller's transaction."""
        self.current(project_id, resource_type, resource_id)
        row = self._row(resource_type, resource_id)
        assert row is not None
        row.version += 1
        self._session.flush()
        return row.version
