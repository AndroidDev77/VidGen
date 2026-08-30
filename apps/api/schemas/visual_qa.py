"""Bounded T20 visual-QA API projections.

Projections are compact on purpose: no provider payloads, no prompts, no image
or video bytes, and no signed URLs. The UI asks the assets route for a
short-lived download URL when it actually needs to display a frame.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract

VisualQATargetLiteral = Literal["keyframe", "video"]


def _default_targets() -> list[VisualQATargetLiteral]:
    return ["keyframe", "video"]


class VisualQARunRequest(StrictContract):
    """Queue or resume QA for a project or one shot."""

    provider: Literal["fake", "openai"] = "fake"
    targets: list[VisualQATargetLiteral] = Field(
        default_factory=_default_targets, min_length=1, max_length=2
    )


class VisualQADecisionRequest(StrictContract):
    reason: str = Field(default="", max_length=500)


class VisualQADimensionProjection(StrictContract):
    dimension: str
    applicable: bool
    raw_score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=100)
    effective_weight: float = Field(ge=0, le=100)
    weighted_contribution: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    warning_codes: list[str] = Field(default_factory=list)
    hard_failure_codes: list[str] = Field(default_factory=list)
    repair_codes: list[str] = Field(default_factory=list)
    finding_summaries: list[str] = Field(default_factory=list)


class VisualQADiagnosticProjection(StrictContract):
    code: str
    outcome: str
    diagnostic_code: str
    measurement: float | None = None
    threshold: float | None = None
    evidence_timestamp_us: int | None = None
    repair_code: str | None = None
    message: str = ""


class VisualQABoundingBoxProjection(StrictContract):
    """Normalized box coordinates only. The stored payload also carries a schema
    version, which is deliberately dropped rather than coerced into a number."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class VisualQAEvidenceProjection(StrictContract):
    evidence_id: UUID
    finding_id: UUID
    evidence_type: str
    sample_id: UUID | None = None
    frame_asset_id: UUID | None = None
    shot_relative_timestamp_us: int | None = None
    source_relative_timestamp_us: int | None = None
    contact_sheet_position: int | None = None
    bounding_box: VisualQABoundingBoxProjection | None = None
    compared_reference_asset_id: UUID | None = None
    confidence: float = Field(ge=0, le=1)
    explanation: str = ""


class VisualQASampleProjection(StrictContract):
    sample_id: UUID
    sequence: int = Field(ge=0)
    sample_type: str
    requested_timestamp_us: int = Field(ge=0)
    actual_timestamp_us: int = Field(ge=0)
    shot_relative_timestamp_us: int = Field(ge=0)
    frame_asset_id: UUID | None = None
    frame_sha256: str
    selection_reason: str
    contact_sheet_position: int | None = None


class VisualQARunProjection(StrictContract):
    """The compact row the dashboard, storyboard grid and inspector list."""

    qa_run_id: UUID
    project_id: UUID
    shot_id: UUID
    target_type: str
    status: str
    outcome: str | None = None
    score: float | None = None
    pass_threshold: float | None = None
    importance: str
    hard_failure: bool = False
    repair_recommendation: str | None = None
    repair_codes: list[str] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)
    confidence: float | None = None
    adjudicated: bool = False
    human_review_decision: str | None = None
    provider: str = ""
    model: str = ""
    cost_microusd: int = Field(default=0, ge=0)
    rubric_version: str
    threshold_version: str
    sampling_version: str
    sample_count: int = Field(default=0, ge=0)
    deterministic_warning_count: int = Field(default=0, ge=0)
    row_version: int = Field(gt=0)
    created_at: datetime
    completed_at: datetime | None = None


class VisualQARunDetailProjection(VisualQARunProjection):
    dimensions: list[VisualQADimensionProjection] = Field(default_factory=list)
    diagnostics: list[VisualQADiagnosticProjection] = Field(default_factory=list)
    samples: list[VisualQASampleProjection] = Field(default_factory=list)
    compared_reference_asset_ids: list[UUID] = Field(default_factory=list)
    contact_sheet_asset_id: UUID | None = None
    report_asset_id: UUID | None = None
    adjudication: dict[str, object] | None = None


class VisualQACollectionResponse(StrictContract):
    project_id: UUID
    items: list[VisualQARunProjection] = Field(default_factory=list)


class VisualQAEvidenceResponse(StrictContract):
    qa_run_id: UUID
    items: list[VisualQAEvidenceProjection] = Field(default_factory=list)
    samples: list[VisualQASampleProjection] = Field(default_factory=list)


class VisualQARunResponse(StrictContract):
    status: Literal["queued"]
    project_id: UUID
    shot_id: UUID | None = None
    targets: list[str] = Field(default_factory=list)
    resource_id: UUID
    row_version: int = Field(gt=0)


class VisualQADecisionResponse(StrictContract):
    """A recorded T20 decision, and the command that carries it to the shot.

    ``continuation_command_id`` is what makes a review decision more than a row
    update: it is the durable command that resumes the waiting shot workflow, or
    starts the replacement run a rejection implies. It is absent only when the
    decision changed nothing the shot has to act on.
    """

    qa_run_id: UUID
    review_id: UUID
    decision: Literal["approved", "rejected"]
    resulting_gate: str
    row_version: int = Field(gt=0)
    continuation_command_id: UUID | None = None
    continuation_command_status: str | None = Field(default=None, max_length=32)
