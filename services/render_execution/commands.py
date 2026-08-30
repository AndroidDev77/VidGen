"""The application-level commands every T17b entry point calls.

Two operations, deliberately separated, because conflating them is what made the
original ``scripts.render_project`` misleading:

* :func:`queue_render_job` validates lineage and creates - or reuses - a durable
  render-job row. It renders nothing.
* :func:`execute_render_job` claims an existing render job and performs the
  render. It creates nothing.

The CLI, the Temporal activity, the out-of-band worker and the Azure Container
Apps Job all call these; none of them has its own implementation.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.render_execution.claims import progress_of
from services.render_execution.executor import RenderExecutionError, RenderExecutor
from services.render_execution.inputs import (
    PIPELINE_VERSION,
    RenderSettings,
    render_settings_for,
    resolve_render_inputs,
)
from services.renderer.selection import RenderLineageError
from vidgen.contracts.render_execution import (
    RenderExecutionProgress,
    RenderExecutionRequest,
    RenderExecutionResult,
    RenderExecutionStatus,
)
from vidgen.db.models import RenderJob
from vidgen.storage.blob import BlobStore

#: Where a render stages its inputs and intermediates. Never inside the blob
#: store, and never part of canonical identity.
DEFAULT_WORK_ROOT = Path(os.getenv("VIDGEN_RENDER_WORK_ROOT", ".local-data/render-work"))


@dataclass(frozen=True, slots=True)
class QueuedRender:
    job: RenderJob
    reused: bool
    input_hash: str
    shot_count: int
    expected_duration_us: int


def default_worker_id() -> str:
    """A worker identity that is stable within a process and unique across them."""
    configured = os.getenv("VIDGEN_RENDER_WORKER_ID")
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"[:128]


def queue_render_job(
    session: Session,
    project_id: UUID,
    *,
    profile: str = "1080p24",
    subtitle_mode: str = "selectable",
    language: str = "en",
    idempotency_key: str | None = None,
) -> QueuedRender:
    """Validate the project's authoritative inputs and queue one render job.

    An existing job for the same input identity is reused rather than duplicated:
    queueing twice for unchanged inputs must not produce two renders. A completed
    job for that identity is returned as-is.
    """
    probe = RenderJob(
        id=uuid4(),
        project_id=project_id,
        status=RenderExecutionStatus.QUEUED.value,
        video_profile={"name": profile},
        caption_profile={"subtitle_mode": subtitle_mode, "language": language},
    )
    settings = render_settings_for(probe)
    resolved = resolve_render_inputs(session, job=probe, settings=settings)
    existing = _existing_job(session, project_id, resolved.input_hash)
    if existing is not None:
        return QueuedRender(
            job=existing,
            reused=True,
            input_hash=resolved.input_hash,
            shot_count=len(resolved.selection.shots),
            expected_duration_us=resolved.total_duration_us,
        )
    job = RenderJob(
        id=probe.id,
        project_id=project_id,
        status=RenderExecutionStatus.QUEUED.value,
        attempt=_next_attempt(session, project_id),
        idempotency_key=idempotency_key or f"t17b:{resolved.input_hash}",
        input_hash=resolved.input_hash,
        input_selection=resolved.contract.model_copy(update={"render_job_id": probe.id}).model_dump(
            mode="json"
        ),
        script_id=resolved.selection.script.id,
        script_version=resolved.selection.script.version,
        narration_run_id=resolved.selection.narration.id,
        storyboard_run_id=resolved.selection.storyboard.id,
        t16_result_reference=f"t16:{resolved.selection.storyboard.id}",
        expected_duration_us=resolved.total_duration_us,
        video_profile={"name": settings.profile},
        audio_profile={"name": "aac-48k-320"},
        caption_profile={"subtitle_mode": settings.subtitle_mode, "language": settings.language},
        pipeline_version=PIPELINE_VERSION,
        renderer_version=PIPELINE_VERSION,
        error={},
    )
    session.add(job)
    session.flush()
    return QueuedRender(
        job=job,
        reused=False,
        input_hash=resolved.input_hash,
        shot_count=len(resolved.selection.shots),
        expected_duration_us=resolved.total_duration_us,
    )


def execute_render_job(
    session: Session,
    blob_store: BlobStore,
    render_job_id: UUID,
    *,
    worker_id: str | None = None,
    work_root: Path | None = None,
    trace_context: dict[str, str] | None = None,
    lease_seconds: int = 300,
    max_attempts: int = 3,
    execution_timeout_seconds: int = 3600,
    minimum_free_bytes: int | None = None,
    preserve_failed_attempts: bool = False,
) -> RenderExecutionResult:
    """Claim and execute one existing render job. The canonical entry point."""
    request = RenderExecutionRequest(
        render_job_id=render_job_id,
        worker_id=worker_id or default_worker_id(),
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        execution_timeout_seconds=execution_timeout_seconds,
        trace_context=dict(trace_context or {}),
    )
    if minimum_free_bytes is not None:
        request = request.model_copy(update={"minimum_free_bytes": minimum_free_bytes})
    executor = RenderExecutor(
        session,
        blob_store,
        work_root=work_root or DEFAULT_WORK_ROOT,
        preserve_failed_attempts=preserve_failed_attempts,
    )
    return executor.execute(request)


def render_progress(session: Session, render_job_id: UUID) -> RenderExecutionProgress:
    """The bounded progress projection for one render job."""
    job = session.get(RenderJob, render_job_id)
    if job is None:
        raise RenderLineageError("render_job_not_found", "the render job does not exist")
    return progress_of(job)


def current_render_job(session: Session, project_id: UUID) -> RenderJob | None:
    """The project's current render: the selected one, else the newest.

    This is the single provider-neutral lookup for "the project's current
    render". T18 download resolution, T22 input selection and any future
    publication stage read the render through this, so there is never a second
    opinion about which render is current.
    """
    selected = session.scalar(
        select(RenderJob).where(RenderJob.project_id == project_id, RenderJob.selected.is_(True))
    )
    if selected is not None:
        return selected
    return session.scalar(
        select(RenderJob)
        .where(RenderJob.project_id == project_id)
        .order_by(RenderJob.created_at.desc(), RenderJob.id.desc())
    )


def completed_render_job(session: Session, project_id: UUID) -> RenderJob | None:
    """The project's current render, but only when it is a finished deliverable.

    A stale, queued, running or failed render is never returned: a caller asking
    for the deliverable must not be handed something that is not one.
    """
    job = current_render_job(session, project_id)
    if job is None:
        return None
    if job.status != RenderExecutionStatus.COMPLETE.value:
        return None
    if job.final_video_asset_id is None or job.verification_report_asset_id is None:
        return None
    return job


def _existing_job(session: Session, project_id: UUID, input_hash: str) -> RenderJob | None:
    return session.scalar(
        select(RenderJob)
        .where(
            RenderJob.project_id == project_id,
            RenderJob.input_hash == input_hash,
            RenderJob.status.not_in(
                [
                    RenderExecutionStatus.FAILED.value,
                    RenderExecutionStatus.CANCELLED.value,
                ]
            ),
        )
        .order_by(RenderJob.created_at.desc(), RenderJob.id.desc())
    )


def _next_attempt(session: Session, project_id: UUID) -> int:
    return (
        len(session.scalars(select(RenderJob.id).where(RenderJob.project_id == project_id)).all())
        + 1
    )


__all__ = [
    "DEFAULT_WORK_ROOT",
    "QueuedRender",
    "RenderExecutionError",
    "RenderSettings",
    "completed_render_job",
    "current_render_job",
    "default_worker_id",
    "execute_render_job",
    "queue_render_job",
    "render_progress",
]
