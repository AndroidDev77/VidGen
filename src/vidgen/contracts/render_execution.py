"""Strict public contracts for T17b render execution.

T17 owns the deterministic rendering library: the immutable
:class:`~vidgen.contracts.render.RenderManifest`, the caption track, the FFmpeg
command plan and the verification report. T17b owns *executing* one queued
render job against that library, and these contracts are the only shapes that
cross the execution boundary - the CLI, the Temporal activity, the out-of-band
worker and the Azure Container Apps Job all speak them.

Nothing here carries credentials, signed URLs, media bytes or unbounded FFmpeg
output: diagnostics are bounded codes and short messages, and every large
artifact is referenced by asset ID.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract
from vidgen.contracts.render import RenderFailure, RenderInputReference

SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RenderExecutionStatus(StrEnum):
    """The durable render-job state machine T17b advances.

    The values are the persisted ``render_jobs.status`` values, so a projection
    never has to translate between a workflow status and a database status.
    ``QUEUED`` is the state T18 and the CLI create; every other value is written
    by the executor.
    """

    QUEUED = "render_queued"
    CLAIMING = "render_claiming"
    PREPARING = "render_preparing"
    MANIFEST_READY = "render_manifest_ready"
    RENDERING = "render_rendering"
    VERIFYING = "render_verifying"
    PERSISTING = "render_persisting"
    COMPLETE = "render_complete"
    FAILED = "render_failed"
    CANCELLED = "render_cancelled"


#: Statuses an executor may claim from. ``render_complete`` is deliberately
#: absent: a completed job is reused, never re-executed.
CLAIMABLE_STATUSES = frozenset(
    {
        RenderExecutionStatus.QUEUED,
        RenderExecutionStatus.CLAIMING,
        RenderExecutionStatus.PREPARING,
        RenderExecutionStatus.MANIFEST_READY,
        RenderExecutionStatus.RENDERING,
        RenderExecutionStatus.VERIFYING,
        RenderExecutionStatus.PERSISTING,
        RenderExecutionStatus.FAILED,
    }
)

TERMINAL_STATUSES = frozenset(
    {
        RenderExecutionStatus.COMPLETE,
        RenderExecutionStatus.FAILED,
        RenderExecutionStatus.CANCELLED,
    }
)

#: The legacy T18 queue status, kept executable so a job queued before T17b - or
#: by the existing review mutation - is still claimable without a data rewrite.
LEGACY_QUEUED_STATUS = "pending"


class RenderExecutionRequest(StrictContract):
    """One execution of one already-queued render job."""

    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=20)
    heartbeat_seconds: int = Field(default=30, ge=5, le=600)
    execution_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    minimum_free_bytes: int = Field(default=10 * 1024**3, ge=0)
    trace_context: dict[str, str] = Field(default_factory=dict, max_length=16)


class RenderInputSelection(StrictContract):
    """The authoritative inputs resolved for one render, and their identity.

    This is persisted before any FFmpeg work begins. Re-resolving the same
    project state must produce the same ``input_hash``; a different hash is a
    materially different render and requires a new render job.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    render_job_id: UUID
    approved_script_id: UUID
    approved_script_version: int = Field(gt=0)
    approved_script_hash: SHA256
    narration_run_id: UUID
    narration_asset_id: UUID
    narration_duration_us: int = Field(gt=0)
    narration_word_timing_hash: SHA256
    storyboard_run_id: UUID
    storyboard_hash: SHA256
    timing_manifest_id: UUID
    timing_manifest_hash: SHA256
    shot_count: int = Field(gt=0, le=500)
    references: list[RenderInputReference] = Field(min_length=1, max_length=2000)
    visual_qa_result_ids: list[UUID] = Field(default_factory=list, max_length=500)
    repair_result_ids: list[UUID] = Field(default_factory=list, max_length=500)
    character_reference_ids: list[UUID] = Field(default_factory=list, max_length=500)
    location_reference_ids: list[UUID] = Field(default_factory=list, max_length=500)
    audio_asset_ids: list[UUID] = Field(default_factory=list, max_length=128)
    subtitle_mode: Literal["selectable", "burn_in", "both"] = "selectable"
    render_profile: str = Field(min_length=1, max_length=32)
    target_duration_us: int = Field(gt=0)
    aspect_ratio: str = Field(pattern=r"^[0-9]{1,2}:[0-9]{1,2}$")
    output_width: int = Field(gt=0, le=16384)
    output_height: int = Field(gt=0, le=16384)
    frame_rate: int = Field(gt=0, le=120)
    caption_configuration_hash: SHA256
    visual_qa_policy_version: str = Field(min_length=1, max_length=64)
    pipeline_version: str = Field(min_length=1, max_length=32)
    input_hash: SHA256
    resolved_at: datetime

    @model_validator(mode="after")
    def utc_timestamps(self) -> RenderInputSelection:
        if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
            raise ValueError("resolved_at must be timezone-aware UTC")
        return self


class RenderExecutionCheckpoint(StrictContract):
    """A durable resume point. Written inside the same transaction as the status."""

    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    status: RenderExecutionStatus
    attempt: int = Field(ge=1)
    progress_percent: int = Field(ge=0, le=100)
    phase: str = Field(min_length=1, max_length=64)
    input_hash: SHA256 | None = None
    manifest_asset_id: UUID | None = None
    caption_asset_id: UUID | None = None
    final_video_asset_id: UUID | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def utc_timestamps(self) -> RenderExecutionCheckpoint:
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware UTC")
        return self


class RenderExecutionProgress(StrictContract):
    """The bounded progress projection the API, the UI and Temporal may read."""

    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    project_id: UUID
    status: RenderExecutionStatus
    progress_percent: int = Field(ge=0, le=100)
    phase: str | None = Field(default=None, max_length=64)
    attempt: int = Field(default=0, ge=0)
    claimed_by: str | None = Field(default=None, max_length=128)
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    failure_code: str | None = Field(default=None, max_length=128)
    failure_classification: str | None = Field(default=None, max_length=32)


class RenderExecutionResult(StrictContract):
    """The canonical outcome of :func:`execute_render_job`.

    Every entry point returns this shape. ``reused`` marks an idempotent
    no-op: the job was already complete, or another worker completed it while
    this one waited.
    """

    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    project_id: UUID
    status: RenderExecutionStatus
    reused: bool = False
    render_identity: SHA256 | None = None
    input_hash: SHA256 | None = None
    output_sha256: SHA256 | None = None
    manifest_asset_id: UUID | None = None
    caption_srt_asset_id: UUID | None = None
    caption_webvtt_asset_id: UUID | None = None
    final_video_asset_id: UUID | None = None
    verification_report_asset_id: UUID | None = None
    measured_duration_us: int | None = Field(default=None, gt=0)
    expected_duration_us: int | None = Field(default=None, gt=0)
    renderer_version: str | None = Field(default=None, max_length=32)
    ffmpeg_version: str | None = Field(default=None, max_length=255)
    attempt: int = Field(default=0, ge=0)
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    failure: RenderFailure | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def completed_jobs_carry_outputs(self) -> RenderExecutionResult:
        if self.status is RenderExecutionStatus.COMPLETE and not all(
            (
                self.render_identity,
                self.manifest_asset_id,
                self.caption_srt_asset_id,
                self.final_video_asset_id,
                self.verification_report_asset_id,
                self.output_sha256,
            )
        ):
            raise ValueError("a complete render must reference every canonical output")
        if self.status is RenderExecutionStatus.FAILED and self.failure is None:
            raise ValueError("a failed render must carry a structured failure")
        return self


class RenderWorkerResult(StrictContract):
    """The compact record the worker prints and turns into a process exit code."""

    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    status: RenderExecutionStatus
    reused: bool = False
    exit_code: int = Field(ge=0, le=125)
    final_video_asset_id: UUID | None = None
    output_sha256: SHA256 | None = None
    measured_duration_us: int | None = Field(default=None, gt=0)
    failure_code: str | None = Field(default=None, max_length=128)
    failure_classification: str | None = Field(default=None, max_length=32)


__all__ = [
    "CLAIMABLE_STATUSES",
    "LEGACY_QUEUED_STATUS",
    "TERMINAL_STATUSES",
    "RenderExecutionCheckpoint",
    "RenderExecutionProgress",
    "RenderExecutionRequest",
    "RenderExecutionResult",
    "RenderExecutionStatus",
    "RenderInputSelection",
    "RenderWorkerResult",
]
