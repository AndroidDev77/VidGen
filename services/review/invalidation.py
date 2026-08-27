"""Downstream invalidation sets for T18 edits and regenerations.

An edit never reruns a stage. It computes the exact set of downstream artifacts
the change makes stale, returns it so the UI can confirm before applying, and
records the marks so the render page can tell a stale render from a current one.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.review import InvalidationEntry, InvalidationSet
from vidgen.db.models import RenderJob
from vidgen.db.narration_models import NarrationRun
from vidgen.db.review_models import DownstreamInvalidation
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord


def _renders(session: Session, project_id: UUID) -> list[RenderJob]:
    return list(
        session.scalars(
            select(RenderJob).where(
                RenderJob.project_id == project_id, RenderJob.status == "render_complete"
            )
        ).all()
    )


def transcript_invalidation_set(session: Session, project_id: UUID) -> InvalidationSet:
    """A transcript edit invalidates the whole generated lineage below it."""
    entries: list[InvalidationEntry] = []
    for script in session.scalars(
        select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
    ).all():
        entries.append(
            InvalidationEntry(
                resource_type="script",
                resource_id=script.id,
                label=f"Script v{script.version}",
                reason="transcript_edited",
            )
        )
    for run in session.scalars(
        select(NarrationRun).where(
            NarrationRun.project_id == project_id, NarrationRun.selected.is_(True)
        )
    ).all():
        entries.append(
            InvalidationEntry(
                resource_type="narration",
                resource_id=run.id,
                label="Selected narration run",
                reason="transcript_edited",
            )
        )
    entries.extend(_storyboard_entries(session, project_id, "transcript_edited"))
    entries.extend(_render_entries(session, project_id, "transcript_edited"))
    return InvalidationSet(entries=entries, requires_confirmation=bool(entries))


def script_invalidation_set(session: Session, project_id: UUID) -> InvalidationSet:
    """A material script change invalidates narration, storyboard, shots and render."""
    entries: list[InvalidationEntry] = []
    for run in session.scalars(
        select(NarrationRun).where(
            NarrationRun.project_id == project_id, NarrationRun.selected.is_(True)
        )
    ).all():
        entries.append(
            InvalidationEntry(
                resource_type="narration",
                resource_id=run.id,
                label="Selected narration run",
                reason="script_edited",
            )
        )
    entries.extend(_storyboard_entries(session, project_id, "script_edited"))
    entries.extend(_render_entries(session, project_id, "script_edited"))
    return InvalidationSet(entries=entries, requires_confirmation=bool(entries))


def shot_invalidation_set(
    session: Session, project_id: UUID, shot: StoryboardShotRecord
) -> InvalidationSet:
    """Regenerating one shot invalidates only that shot's render dependency.

    Sibling shots keep their locked identities, attempts and selected assets, so
    they never appear in this set.
    """
    entries = [
        InvalidationEntry(
            resource_type="shot",
            resource_id=shot.id,
            label=f"Shot {shot.global_sequence + 1}",
            reason="shot_regenerated",
        )
    ]
    entries.extend(_render_entries(session, project_id, "shot_regenerated"))
    return InvalidationSet(entries=entries, requires_confirmation=True)


def _storyboard_entries(session: Session, project_id: UUID, reason: str) -> list[InvalidationEntry]:
    entries: list[InvalidationEntry] = []
    for run in session.scalars(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    ).all():
        entries.append(
            InvalidationEntry(
                resource_type="storyboard",
                resource_id=run.id,
                label=f"Storyboard v{run.version} ({run.shot_count} shots)",
                reason=reason,
            )
        )
    return entries


def _render_entries(session: Session, project_id: UUID, reason: str) -> list[InvalidationEntry]:
    return [
        InvalidationEntry(
            resource_type="render",
            resource_id=render.id,
            label=f"Verified render attempt {render.attempt}",
            reason=reason,
        )
        for render in _renders(session, project_id)
    ]


class InvalidationRecorder:
    """Persist an invalidation set; previous outputs are marked stale, never deleted."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        project_id: UUID,
        origin_type: str,
        origin_id: UUID,
        invalidation: InvalidationSet,
    ) -> None:
        for entry in invalidation.entries:
            existing = self._session.scalar(
                select(DownstreamInvalidation).where(
                    DownstreamInvalidation.project_id == project_id,
                    DownstreamInvalidation.origin_type == origin_type,
                    DownstreamInvalidation.origin_id == origin_id,
                    DownstreamInvalidation.invalidated_type == entry.resource_type,
                    DownstreamInvalidation.invalidated_id == entry.resource_id,
                )
            )
            if existing is not None:
                continue
            self._session.add(
                DownstreamInvalidation(
                    project_id=project_id,
                    origin_type=origin_type,
                    origin_id=origin_id,
                    invalidated_type=entry.resource_type,
                    invalidated_id=entry.resource_id,
                    reason=entry.reason,
                )
            )
        self._session.flush()
