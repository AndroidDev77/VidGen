"""Bounded T22 final editorial-QA API projections.

Projections are compact on purpose: no provider payloads, no prompts, no media
bytes and no signed URLs. The dashboard asks the assets route for a short-lived
download URL when it actually needs to show a frame or a contact sheet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract

FinalDecisionLiteral = Literal["PASS", "FAIL", "REVIEW"]
FinalReviewDecisionLiteral = Literal["accept", "reject", "escalate"]


class FinalEditorialRunRequest(StrictContract):
    """Start or resume final QA for the project's current render."""

    provider: Literal["fake", "openai"] = "fake"
    adjudicate: bool = True


class FinalEditorialCancelRequest(StrictContract):
    reason: str = Field(default="", max_length=500)


class FinalEditorialReviewRequest(StrictContract):
    finding_id: UUID
    decision: FinalReviewDecisionLiteral
    reason_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class FinalEditorialRemediationRequest(StrictContract):
    """Route confirmed findings to the existing stage that owns the repair."""

    target: str = Field(min_length=1, max_length=64)
    finding_ids: list[UUID] = Field(min_length=1, max_length=64)
    #: Required for a shot-scoped remediation: the shot whose repair this is.
    shot_id: UUID | None = None


class FinalMeasurementProjection(StrictContract):
    container_format: str = ""
    byte_size: int = Field(default=0, ge=0)
    video_codec: str = ""
    audio_codec: str = ""
    width: int | None = None
    height: int | None = None
    pixel_format: str = ""
    frame_rate: str = ""
    container_duration_us: int | None = None
    video_duration_us: int | None = None
    audio_duration_us: int | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    clipping_ratio: float | None = None
    video_decoded: bool = False
    audio_decoded: bool = False
    black_interval_count: int = Field(default=0, ge=0)
    freeze_interval_count: int = Field(default=0, ge=0)
    silence_interval_count: int = Field(default=0, ge=0)
    ffmpeg_version: str = ""
    ffprobe_version: str = ""


class FinalCheckProjection(StrictContract):
    check_id: UUID
    check_type: str
    code: str
    status: str
    blocking: bool
    measurement: float | None = None
    threshold: float | None = None
    unit: str = ""
    start_us: int | None = None
    end_us: int | None = None
    cue_sequence: int | None = None
    tool: str = ""
    tool_version: str = ""
    message: str = ""


class FinalDimensionProjection(StrictContract):
    category: str
    applicable: bool = True
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    warning_finding_count: int = Field(default=0, ge=0)
    summary: str = ""


class FinalEvidenceProjection(StrictContract):
    evidence_id: UUID
    evidence_type: str
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    frame_asset_id: UUID | None = None
    sample_id: UUID | None = None
    contact_sheet_asset_id: UUID | None = None
    contact_sheet_position: int | None = None
    caption_cue_sequence: int | None = None
    shot_id: UUID | None = None
    measurement: float | None = None
    threshold: float | None = None
    explanation: str = ""


class FinalFindingProjection(StrictContract):
    """One timeline marker the dashboard renders, with everything it points at."""

    finding_id: UUID
    category: str
    severity: str
    blocking: bool
    confidence: float = Field(ge=0, le=1)
    issue_code: str
    summary: str
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    shot_ids: list[UUID] = Field(default_factory=list)
    caption_cue_sequences: list[int] = Field(default_factory=list)
    narration_segment_ids: list[UUID] = Field(default_factory=list)
    evidence: list[FinalEvidenceProjection] = Field(default_factory=list)
    expected_behavior: str = ""
    observed_behavior: str = ""
    remediation_target: str = "NONE"
    provenance: str = "deterministic"
    resolved_by_review: bool = False


class FinalRemediationProjection(StrictContract):
    target: str
    finding_ids: list[UUID] = Field(default_factory=list)
    shot_ids: list[UUID] = Field(default_factory=list)
    caption_cue_sequences: list[int] = Field(default_factory=list)
    reason: str = ""
    requires_new_render: bool = True


class FinalEditorialRunProjection(StrictContract):
    """The compact status every dashboard view needs."""

    final_editorial_run_id: UUID
    project_id: UUID
    final_render_asset_id: UUID
    render_manifest_asset_id: UUID
    render_identity: str
    final_qa_identity: str
    input_hash: str
    configuration_hash: str
    report_version: str = ""
    status: str
    phase: str
    decision: FinalDecisionLiteral | None = None
    selected: bool = False
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    warning_finding_count: int = Field(default=0, ge=0)
    deterministic_failure_count: int = Field(default=0, ge=0)
    remediation_targets: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    adjudicated: bool = False
    cost_microusd: int = Field(default=0, ge=0)
    report_asset_id: UUID | None = None
    contact_sheet_asset_id: UUID | None = None
    error_code: str | None = None
    row_version: int = Field(ge=0)
    created_at: datetime
    completed_at: datetime | None = None


class FinalEditorialRunDetailProjection(FinalEditorialRunProjection):
    """Everything the final-review page renders for one run."""

    measurements: FinalMeasurementProjection | None = None
    media_checks: list[FinalCheckProjection] = Field(default_factory=list)
    audio_checks: list[FinalCheckProjection] = Field(default_factory=list)
    caption_checks: list[FinalCheckProjection] = Field(default_factory=list)
    dimensions: list[FinalDimensionProjection] = Field(default_factory=list)
    findings: list[FinalFindingProjection] = Field(default_factory=list)
    remediation_routes: list[FinalRemediationProjection] = Field(default_factory=list)
    adjudication_confidence: float | None = None
    adjudication_decided: bool = False
    gate_reasons: list[str] = Field(default_factory=list)
    timeline_duration_us: int = Field(default=0, ge=0)


class FinalEditorialCollectionResponse(StrictContract):
    project_id: UUID
    items: list[FinalEditorialRunProjection] = Field(default_factory=list)


class FinalEditorialRunResponse(StrictContract):
    """A manual T22 run, and the durable command that will execute it.

    ``resource_id`` is the command ID, so ``queued`` names a row a dispatcher
    can claim rather than a value derived from the request.
    """

    status: Literal["queued", "cancelled"]
    project_id: UUID
    final_render_asset_id: UUID | None = None
    provider: str = "fake"
    resource_id: UUID
    row_version: int = Field(ge=0)
    command_id: UUID | None = None
    command_status: str | None = Field(default=None, max_length=32)
    workflow_id: str | None = Field(default=None, max_length=255)


class FinalEditorialReviewResponse(StrictContract):
    final_editorial_run_id: UUID
    review_id: UUID
    finding_id: UUID
    decision: FinalReviewDecisionLiteral
    resulting_gate: FinalDecisionLiteral
    row_version: int = Field(ge=0)


class FinalEditorialRemediationResponse(StrictContract):
    """Where confirmed findings were routed, and what is executing that route."""

    final_editorial_run_id: UUID
    target: str
    routed_finding_ids: list[UUID] = Field(default_factory=list)
    requires_new_render: bool = True
    resource_id: UUID
    row_version: int = Field(ge=0)
    command_id: UUID | None = None
    command_status: str | None = Field(default=None, max_length=32)


class FinalCompletionGateProjection(StrictContract):
    """Whether the project may reach its final completed state, and why not."""

    project_id: UUID
    final_editorial_run_id: UUID | None = None
    final_render_asset_id: UUID | None = None
    decision: FinalDecisionLiteral | None = None
    allowed: bool = False
    reason: str
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    deterministic_failure_count: int = Field(default=0, ge=0)
    gate_version: str = ""
    #: The project row version a client must echo back to start or resume a run.
    row_version: int = Field(default=0, ge=0)
