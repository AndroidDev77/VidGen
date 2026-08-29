"""Transactional render-job claiming, leases, heartbeats and checkpoints.

Only one worker may actively execute a render job. That is enforced with a
conditional ``UPDATE`` - not a process-local lock, not an advisory lock, and not
an assumption that only one worker exists - so it holds identically for a local
CLI run, a Temporal activity retry and two Container Apps Job replicas that the
platform started for the same job.

The rules the rest of T17b depends on:

* A claim is a conditional update. Exactly one concurrent caller wins.
* A live lease belongs to its holder. Another worker is refused until it expires.
* A heartbeat extends the lease, so a long FFmpeg encode never loses it.
* A stale lease is reclaimable, so a worker that died does not lock the job.
* A completed job is never claimed; it is reused.
* Attempts are bounded, so a job that fails deterministically stops retrying.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from vidgen.contracts.render_execution import (
    CLAIMABLE_STATUSES,
    LEGACY_QUEUED_STATUS,
    RenderExecutionCheckpoint,
    RenderExecutionProgress,
    RenderExecutionStatus,
)
from vidgen.db.models import RenderJob

CLAIMABLE_DATABASE_STATUSES: frozenset[str] = frozenset(
    {status.value for status in CLAIMABLE_STATUSES} | {LEGACY_QUEUED_STATUS}
)


class RenderClaimError(RuntimeError):
    """A structured, non-retryable refusal to claim a render job."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Claim:
    render_job_id: UUID
    worker_id: str
    attempt: int
    lease_expires_at: datetime


def _rows_updated(result: Any) -> int:
    """The affected-row count of a conditional UPDATE.

    SQLAlchemy types ``Session.execute`` as the generic ``Result``; the DML
    cursor result that actually comes back carries ``rowcount``, and that count
    is the entire concurrency proof, so it is read through one narrow helper
    rather than ignored at every call site.
    """
    return int(result.rowcount)


def _expire(session: Session, render_job_id: UUID) -> None:
    """Drop the identity-map copy of a row a conditional UPDATE just changed.

    The claim and heartbeat updates deliberately run with
    ``synchronize_session=False``: the whole point of the ``WHERE`` clause is
    that the database, not this session, decides whether the update applied.
    Expiring the row afterwards is what makes the next read see that decision.
    """
    tracked = session.get(RenderJob, render_job_id)
    if tracked is not None:
        session.expire(tracked)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def claim_render_job(
    session: Session,
    *,
    render_job_id: UUID,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> Claim:
    """Claim a queued or reclaimable render job for exactly one worker.

    The claim is a single conditional ``UPDATE``: it matches only a job whose
    lease is absent, expired, or already held by this worker. A second worker
    running the same statement at the same time updates zero rows and is told
    the job is held.
    """
    moment = now or utcnow()
    job = session.get(RenderJob, render_job_id)
    if job is None:
        raise RenderClaimError("render_job_not_found", "the render job does not exist")
    # Read the row as the database currently holds it. Another worker - or this
    # session's own conditional updates - may have moved it since it was last
    # loaded, and a claim decided from a stale identity-map copy is not a claim.
    session.refresh(job)
    if job.status == RenderExecutionStatus.COMPLETE.value:
        raise RenderClaimError("render_job_complete", "the render job is already complete")
    if job.status == RenderExecutionStatus.CANCELLED.value or job.cancel_requested:
        raise RenderClaimError("render_job_cancelled", "the render job is cancelled")
    if job.status not in CLAIMABLE_DATABASE_STATUSES:
        raise RenderClaimError(
            "render_job_not_claimable",
            f"a render job in status {job.status!r} cannot be executed",
        )
    if job.attempt_count >= max_attempts:
        raise RenderClaimError(
            "render_attempts_exhausted",
            "the render job has used its bounded attempt budget",
        )
    lease_expires_at = moment + timedelta(seconds=lease_seconds)
    attempt = job.attempt_count + 1
    result = session.execute(
        update(RenderJob)
        .where(
            RenderJob.id == render_job_id,
            RenderJob.status.in_(sorted(CLAIMABLE_DATABASE_STATUSES)),
            RenderJob.cancel_requested.is_(False),
            (RenderJob.lease_expires_at.is_(None))
            | (RenderJob.lease_expires_at <= moment)
            | (RenderJob.claimed_by == worker_id),
        )
        .execution_options(synchronize_session=False)
        .values(
            status=RenderExecutionStatus.CLAIMING.value,
            claimed_by=worker_id,
            claimed_at=moment,
            lease_expires_at=lease_expires_at,
            heartbeat_at=moment,
            attempt_count=attempt,
            attempt=max(job.attempt, attempt),
            checkpoint=RenderExecutionStatus.CLAIMING.value,
            error_code=None,
            failure_classification=None,
            started_at=job.started_at or moment,
        )
    )
    if _rows_updated(result) != 1:
        raise RenderClaimError(
            "render_job_leased", "the render job is held by another active executor"
        )
    session.flush()
    session.expire(job)
    session.refresh(job)
    return Claim(
        render_job_id=render_job_id,
        worker_id=worker_id,
        attempt=attempt,
        lease_expires_at=lease_expires_at,
    )


def heartbeat(
    session: Session,
    *,
    claim: Claim,
    lease_seconds: int,
    progress_percent: int | None = None,
    phase: str | None = None,
    now: datetime | None = None,
) -> datetime:
    """Extend this worker's lease. Raises when the lease was lost or revoked."""
    moment = now or utcnow()
    expires = moment + timedelta(seconds=lease_seconds)
    values: dict[str, object] = {"heartbeat_at": moment, "lease_expires_at": expires}
    if progress_percent is not None:
        values["progress_percent"] = max(0, min(100, progress_percent))
    if phase is not None:
        values["checkpoint"] = phase[:64]
    result = session.execute(
        update(RenderJob)
        .where(RenderJob.id == claim.render_job_id, RenderJob.claimed_by == claim.worker_id)
        .execution_options(synchronize_session=False)
        .values(**values)
    )
    if _rows_updated(result) != 1:
        raise RenderClaimError("render_lease_lost", "this worker no longer holds the render lease")
    session.flush()
    _expire(session, claim.render_job_id)
    return expires


def require_lease(session: Session, claim: Claim) -> RenderJob:
    """Load the job and prove this worker still holds it and it is not cancelled."""
    job = session.get(RenderJob, claim.render_job_id)
    if job is None:
        raise RenderClaimError("render_job_not_found", "the render job disappeared mid-execution")
    if job.claimed_by != claim.worker_id:
        raise RenderClaimError("render_lease_lost", "this worker no longer holds the render lease")
    return job


def release(session: Session, claim: Claim) -> None:
    """Drop the lease without changing the job's status.

    A failed worker that reaches this path leaves the job immediately
    reclaimable instead of waiting out its lease.
    """
    session.execute(
        update(RenderJob)
        .where(RenderJob.id == claim.render_job_id, RenderJob.claimed_by == claim.worker_id)
        .execution_options(synchronize_session=False)
        .values(claimed_by=None, lease_expires_at=None)
    )
    session.flush()
    _expire(session, claim.render_job_id)


def checkpoint(
    session: Session,
    *,
    claim: Claim,
    status: RenderExecutionStatus,
    phase: str,
    progress_percent: int,
    now: datetime | None = None,
) -> RenderExecutionCheckpoint:
    """Advance the durable checkpoint. The caller commits it."""
    moment = now or utcnow()
    job = require_lease(session, claim)
    job.status = status.value
    job.checkpoint = phase[:64]
    job.progress_percent = max(0, min(100, progress_percent))
    job.heartbeat_at = moment
    session.flush()
    return RenderExecutionCheckpoint(
        render_job_id=job.id,
        status=status,
        attempt=max(job.attempt_count, 1),
        progress_percent=job.progress_percent,
        phase=job.checkpoint or phase,
        input_hash=job.input_hash,
        manifest_asset_id=job.manifest_asset_id,
        caption_asset_id=job.srt_asset_id,
        final_video_asset_id=job.final_video_asset_id,
        updated_at=moment,
    )


def request_cancellation(session: Session, render_job_id: UUID) -> bool:
    """Ask the executor holding this job to stop at its next safe point."""
    result = session.execute(
        update(RenderJob)
        .where(
            RenderJob.id == render_job_id,
            RenderJob.status.not_in(
                [
                    RenderExecutionStatus.COMPLETE.value,
                    RenderExecutionStatus.CANCELLED.value,
                ]
            ),
        )
        .execution_options(synchronize_session=False)
        .values(cancel_requested=True)
    )
    session.flush()
    session.expire_all()
    return _rows_updated(result) == 1


def progress_of(job: RenderJob) -> RenderExecutionProgress:
    """The bounded progress projection T18, Temporal and the CLI read."""
    try:
        status = RenderExecutionStatus(job.status)
    except ValueError:
        status = (
            RenderExecutionStatus.COMPLETE
            if job.status == RenderExecutionStatus.COMPLETE.value
            else RenderExecutionStatus.QUEUED
        )
    return RenderExecutionProgress(
        render_job_id=job.id,
        project_id=job.project_id,
        status=status,
        progress_percent=max(0, min(100, job.progress_percent)),
        phase=job.checkpoint,
        attempt=max(job.attempt_count, 0),
        claimed_by=job.claimed_by,
        lease_expires_at=_aware(job.lease_expires_at),
        heartbeat_at=_aware(job.heartbeat_at),
        cancel_requested=job.cancel_requested,
        failure_code=job.error_code,
        failure_classification=job.failure_classification,
    )
