"""T18 review-UI control-plane projections.

Every contract here is a bounded projection: identifiers, statuses, counts,
durations, hashes, and short codes. Media bytes, prompts, provider payloads and
signed URLs never appear in these shapes, and the event contracts additionally
exclude transcript and script text so a Server-Sent Events stream stays small.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from vidgen.contracts.common import StrictContract

SCHEMA_VERSION = "1.0"


class ApiErrorCode(StrEnum):
    """Stable machine-readable failure codes rendered by the review UI."""

    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    PRECONDITION_REQUIRED = "precondition_required"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_KEY_MISMATCH = "idempotency_key_mismatch"
    WORKFLOW_NOT_STARTED = "workflow_not_started"
    UPLOAD_INCOMPLETE = "upload_incomplete"
    RENDER_NOT_VERIFIED = "render_not_verified"
    RENDER_STALE = "render_stale"
    SHOT_NOT_RETRYABLE = "shot_not_retryable"
    #: T18b preconditions. A workflow that cannot narrate must be refused
    #: before Temporal is involved, and a routing-only remediation target must
    #: say so rather than answer 202 to work nothing will execute.
    VOICE_PROFILE_REQUIRED = "voice_profile_required"
    COMMAND_UPSTREAM_STALE = "command_upstream_stale"
    REMEDIATION_UNSUPPORTED = "remediation_unsupported"
    ATTEMPT_NOT_ELIGIBLE = "attempt_not_eligible"
    BUDGET_DENIED = "budget_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


class ApiErrorField(StrictContract):
    field: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=500)


class ApiError(StrictContract):
    """The single structured failure body every T18 route returns."""

    schema_version: Literal["1.0"] = "1.0"
    code: ApiErrorCode
    summary: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    current_version: int | None = Field(default=None, ge=1)
    workflow_id: str | None = Field(default=None, max_length=255)
    stage: str | None = Field(default=None, max_length=64)
    fields: list[ApiErrorField] = Field(default_factory=list, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=128)
    # The originating domain code, where a route raises one that is narrower
    # than ``code`` (for example the T05 upload validation codes).
    detail_code: str | None = Field(default=None, max_length=128)


class PipelineStage(StrEnum):
    """Known pipeline stages in repository order, as shown by the timeline."""

    UPLOAD = "upload"
    MEDIA_PROCESSING = "media_processing"
    TRANSCRIPT_ACQUISITION = "transcript_acquisition"
    EVIDENCE = "evidence"
    EPISODE_ANALYSIS = "episode_analysis"
    SCRIPT_GENERATION = "script_generation"
    NARRATION = "narration"
    STORYBOARD = "storyboard"
    KEYFRAMES = "keyframes"
    ANIMATION = "animation"
    SHOT_ORCHESTRATION = "shot_orchestration"
    CAPTIONS = "captions"
    RENDERING = "rendering"
    REVIEW = "review"


PIPELINE_STAGE_ORDER: tuple[PipelineStage, ...] = tuple(PipelineStage)


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class StageTimelineEntry(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: PipelineStage
    state: StageState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    detail_code: str | None = Field(default=None, max_length=64)


class WorkflowStatusProjection(StrictContract):
    """Compact workflow status; never carries stage payloads."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    workflow_id: str | None = Field(default=None, max_length=255)
    run_id: str | None = Field(default=None, max_length=255)
    status: str = Field(max_length=64)
    current_stage: PipelineStage | None = None
    completed_stages: list[PipelineStage] = Field(default_factory=list, max_length=32)
    cancelled: bool = False
    started_at: datetime | None = None
    updated_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    total_shot_count: int = Field(default=0, ge=0)
    completed_shot_count: int = Field(default=0, ge=0)
    failed_shot_count: int = Field(default=0, ge=0)
    retryable_failure_count: int = Field(default=0, ge=0)
    render_status: str | None = Field(default=None, max_length=64)
    stages: list[StageTimelineEntry] = Field(default_factory=list, max_length=32)
    # ``None`` whenever the backend cannot honestly compute a percentage.
    progress_percentage: float | None = Field(default=None, ge=0, le=100)

    @field_validator("started_at", "updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("workflow timestamps must be timezone-aware")
        return value


class ProjectEventProjection(StrictContract):
    """One deduplicated, ordered Server-Sent Event payload."""

    schema_version: Literal["1.0"] = "1.0"
    event_id: int = Field(ge=1)
    project_id: UUID
    workflow_id: str | None = Field(default=None, max_length=255)
    event_type: str = Field(max_length=64)
    stage: PipelineStage | None = None
    status: str = Field(max_length=64)
    progress_percentage: float | None = Field(default=None, ge=0, le=100)
    completed_shot_count: int | None = Field(default=None, ge=0)
    total_shot_count: int | None = Field(default=None, ge=0)
    retryable_failure_count: int | None = Field(default=None, ge=0)
    render_status: str | None = Field(default=None, max_length=64)
    cost_summary_version: int | None = Field(default=None, ge=0)
    warning_code: str | None = Field(default=None, max_length=64)
    failure_code: str | None = Field(default=None, max_length=64)
    created_at: datetime


class TranscriptSegmentProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    segment_id: UUID
    sequence: int = Field(ge=0)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str
    speaker_label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    edited: bool = False
    row_version: int = Field(ge=1)


class TranscriptProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    transcript_id: UUID
    project_id: UUID
    version: int = Field(ge=1)
    language: str | None = None
    origin: Literal["transcription", "subtitle"]
    duration_seconds: float = Field(gt=0)
    coverage_score: float = Field(ge=0, le=1)
    selected: bool
    row_version: int = Field(ge=1)
    source_asset_id: UUID | None = None
    segments: list[TranscriptSegmentProjection] = Field(default_factory=list)


class ScriptSegmentProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    segment_id: UUID
    stable_segment_id: UUID
    sequence: int = Field(ge=0)
    segment_type: str = Field(max_length=16)
    speaker_kind: str = Field(max_length=16)
    speaker_label: str | None = None
    text: str
    visual_gag: str | None = None
    joke_annotation_count: int = Field(ge=0)
    plot_beat_ids: list[str] = Field(default_factory=list)
    word_count: int = Field(ge=0)
    estimated_duration_ms: int = Field(gt=0)
    measured_narration_duration_ms: int | None = Field(default=None, ge=0)
    locked: bool = False
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    row_version: int = Field(ge=1)


class ScriptSummaryProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    script_id: UUID
    version: int = Field(ge=1)
    status: str = Field(max_length=64)
    selected: bool
    actual_word_count: int = Field(ge=0)
    target_word_count: int = Field(gt=0)
    target_duration_ms: int = Field(gt=0)
    parent_script_id: UUID | None = None
    created_at: datetime
    row_version: int = Field(ge=1)


class ScriptProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    script: ScriptSummaryProjection
    approved: bool
    segments: list[ScriptSegmentProjection] = Field(default_factory=list)


class InvalidationEntry(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    resource_type: str = Field(max_length=64)
    resource_id: UUID
    label: str = Field(max_length=255)
    reason: str = Field(max_length=128)


class InvalidationSet(StrictContract):
    """The exact downstream lineage a mutation will mark stale."""

    schema_version: Literal["1.0"] = "1.0"
    entries: list[InvalidationEntry] = Field(default_factory=list, max_length=512)
    requires_confirmation: bool = False


class StoryboardShotProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    stable_shot_id: UUID
    global_sequence: int = Field(ge=0)
    segment_sequence: int = Field(ge=0)
    script_segment_id: UUID
    global_start_us: int = Field(ge=0)
    global_end_us: int = Field(gt=0)
    usable_duration_us: int = Field(gt=0)
    requested_generation_duration_us: int = Field(gt=0)
    trim_start_us: int = Field(ge=0)
    trim_end_us: int = Field(ge=0)
    visual_objective: str = Field(max_length=2000)
    camera_framing: str | None = Field(default=None, max_length=64)
    camera_movement: str | None = Field(default=None, max_length=64)
    character_references: list[str] = Field(default_factory=list, max_length=32)
    location_reference: str | None = Field(default=None, max_length=255)
    transition_in: str | None = Field(default=None, max_length=64)
    transition_out: str | None = Field(default=None, max_length=64)
    workflow_status: str = Field(max_length=64)
    selected_keyframe_asset_id: UUID | None = None
    selected_video_asset_id: UUID | None = None
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    attempt_count: int = Field(default=0, ge=0)
    cost_amount: str | None = Field(default=None, max_length=32)
    warning_code: str | None = Field(default=None, max_length=64)
    failure_code: str | None = Field(default=None, max_length=64)
    row_version: int = Field(ge=1)


class StoryboardProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_run_id: UUID
    version: int = Field(ge=1)
    selected: bool
    shot_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    total_duration_us: int = Field(ge=0)
    timing_manifest_asset_id: UUID | None = None
    row_version: int = Field(ge=1)
    shots: list[StoryboardShotProjection] = Field(default_factory=list)


class ShotAttemptProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID
    kind: Literal["keyframe", "video"]
    attempt_number: int = Field(ge=1)
    status: str = Field(max_length=64)
    asset_id: UUID | None = None
    provider: str = Field(max_length=64)
    model: str = Field(max_length=128)
    provider_task_id: str | None = Field(default=None, max_length=255)
    generation_identity: str | None = Field(default=None, max_length=64)
    prompt_version: str | None = Field(default=None, max_length=32)
    generated_duration_us: int | None = Field(default=None, ge=0)
    usable_duration_us: int | None = Field(default=None, ge=0)
    cost_amount: str | None = Field(default=None, max_length=32)
    failure_class: str | None = Field(default=None, max_length=64)
    selected: bool = False
    created_at: datetime


class ShotDetailProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot: StoryboardShotProjection
    child_workflow_id: str | None = Field(default=None, max_length=255)
    child_workflow_status: str = Field(max_length=64)
    child_workflow_retryable: bool = False
    identity_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    trim_instructions_asset_id: UUID | None = None
    source_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    keyframe_attempts: list[ShotAttemptProjection] = Field(default_factory=list)
    video_attempts: list[ShotAttemptProjection] = Field(default_factory=list)
    regeneration_history: list[str] = Field(default_factory=list, max_length=64)


class ShotStatusProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    child_workflow_id: str | None = Field(default=None, max_length=255)
    status: str = Field(max_length=64)
    retryable: bool = False
    attempt_count: int = Field(ge=0)
    failure_code: str | None = Field(default=None, max_length=64)
    row_version: int = Field(ge=1)


class ShotRegenerationResult(StrictContract):
    """What regenerating one shot actually created.

    ``child_workflow_id`` is deliberately optional and deliberately empty until
    the dispatcher has started the replacement child: before T18b this field
    carried a workflow ID that had been *calculated* and never started, which is
    exactly the class of untruth T18b removes. ``command_id`` is what the caller
    polls in the meantime, and it always exists.
    """

    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    #: The replacement child's real Temporal ID, once one has been started.
    child_workflow_id: str | None = Field(default=None, max_length=255)
    new_identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_identity_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    preserved_attempt_ids: list[UUID] = Field(default_factory=list, max_length=64)
    invalidation: InvalidationSet
    row_version: int = Field(ge=1)
    #: The durable command driving this regeneration. Always present.
    command_id: UUID | None = None
    command_status: str | None = Field(default=None, max_length=32)
    #: How many times this shot has been deliberately regenerated. Part of the
    #: replacement child's reproducible identity.
    regeneration_sequence: int = Field(default=0, ge=0)


class RenderProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    render_job_id: UUID
    project_id: UUID
    status: str = Field(max_length=64)
    attempt: int = Field(ge=1)
    render_version: str = Field(max_length=32)
    render_identity: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    selected: bool
    stale: bool
    verified: bool
    verification_summary: str | None = Field(default=None, max_length=500)
    expected_duration_us: int | None = Field(default=None, gt=0)
    measured_duration_us: int | None = Field(default=None, gt=0)
    selected_shot_count: int = Field(default=0, ge=0)
    caption_language: str | None = Field(default=None, max_length=16)
    caption_cue_count: int | None = Field(default=None, ge=0)
    subtitle_mode: str = Field(default="external", max_length=32)
    integrated_loudness_lufs: float | None = None
    true_peak_dbtp: float | None = None
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    final_video_asset_id: UUID | None = None
    srt_asset_id: UUID | None = None
    webvtt_asset_id: UUID | None = None
    verification_report_asset_id: UUID | None = None
    manifest_asset_id: UUID | None = None
    script_id: UUID | None = None
    script_version: int | None = Field(default=None, ge=1)
    storyboard_run_id: UUID | None = None
    narration_run_id: UUID | None = None
    ffmpeg_version: str | None = Field(default=None, max_length=255)
    lineage_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    # T17b execution state. The dashboard shows real render progress and the
    # real reason a render failed, rather than a row that says "pending" for as
    # long as the encode takes.
    progress_percent: int = Field(default=0, ge=0, le=100)
    checkpoint: str | None = Field(default=None, max_length=64)
    attempt_count: int = Field(default=0, ge=0)
    cancel_requested: bool = False
    failure_code: str | None = Field(default=None, max_length=128)
    failure_classification: str | None = Field(default=None, max_length=32)
    output_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    renderer_version: str | None = Field(default=None, max_length=32)
    #: True only for a complete, verified, non-stale render with a stored final
    #: asset. A queued, running, failed or stale render is never downloadable.
    downloadable: bool = False
    approval: RenderApprovalProjection | None = None
    row_version: int = Field(ge=1)
    completed_at: datetime | None = None


class RenderApprovalProjection(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: UUID
    render_job_id: UUID
    approved_by: str = Field(max_length=255)
    approved_at: datetime
    lineage_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    applies_to_current_lineage: bool


class ProjectSummaryProjection(StrictContract):
    """The project-list row; costs stay exact decimal strings."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    name: str = Field(max_length=255)
    status: str = Field(max_length=64)
    current_stage: PipelineStage | None = None
    progress_percentage: float | None = Field(default=None, ge=0, le=100)
    target_duration_seconds: float = Field(gt=0)
    visual_style: str
    humor_intensity: int = Field(ge=0, le=10)
    updated_at: datetime
    committed_cost_amount: str | None = Field(default=None, max_length=32)
    hard_cap_amount: str | None = Field(default=None, max_length=32)
    has_failures: bool = False
    row_version: int = Field(ge=1)


RenderProjection.model_rebuild()
