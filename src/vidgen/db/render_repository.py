"""Transactional T17 job, attempt, and caption persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import RenderJob
from vidgen.db.render_models import CaptionTrackRecord, RenderAttempt


class RenderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_identity(self, render_identity: str) -> RenderJob | None:
        return self.session.scalar(
            select(RenderJob).where(RenderJob.render_identity == render_identity)
        )

    def completed_by_identity(self, render_identity: str) -> RenderJob | None:
        return self.session.scalar(
            select(RenderJob).where(
                RenderJob.render_identity == render_identity,
                RenderJob.status == "render_complete",
                RenderJob.final_video_asset_id.is_not(None),
                RenderJob.verification_report_asset_id.is_not(None),
            )
        )

    def create_or_resume(self, job: RenderJob) -> tuple[RenderJob, bool]:
        existing = self.by_identity(job.render_identity or "")
        if existing is not None:
            return existing, True
        self.session.add(job)
        self.session.flush()
        return job, False

    def next_attempt(self, job: RenderJob, manifest_hash: str) -> RenderAttempt:
        attempts = self.session.scalars(
            select(RenderAttempt).where(RenderAttempt.render_job_id == job.id)
        ).all()
        attempt = RenderAttempt(
            render_job_id=job.id,
            attempt_number=len(attempts) + 1,
            status="render_staging",
            manifest_hash=manifest_hash,
            operational_metadata={},
            started_at=datetime.now(UTC),
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def checkpoint(self, job: RenderJob, status: str, *, error_code: str | None = None) -> None:
        job.status = status
        job.error_code = error_code
        if job.started_at is None:
            job.started_at = datetime.now(UTC)
        if status in {"render_complete", "render_failed", "render_cancelled"}:
            job.completed_at = datetime.now(UTC)
        self.session.flush()

    def caption_for_job(self, render_job_id: UUID) -> CaptionTrackRecord | None:
        return self.session.scalar(
            select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == render_job_id)
        )
