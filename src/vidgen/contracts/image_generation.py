"""Strict provider-neutral contracts for T14 keyframe generation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

from vidgen.contracts.common import StrictContract

Sha256 = str


class KeyframeRole(StrEnum):
    FIRST_FRAME = "FIRST_FRAME"
    LAST_FRAME = "LAST_FRAME"


class ImageQuality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImageFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"


class ImageReferenceBinding(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantic_role: Literal["source", "style", "character", "location", "approved"]
    required: bool = False
    order: int = Field(ge=0)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


class VisualIntent(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    shot_sequence: int = Field(ge=0)
    keyframe_role: KeyframeRole
    visual_purpose: str = Field(min_length=1)
    style_lock: str = Field(min_length=1)
    visible_character_count: int = Field(ge=0)
    character_descriptions: list[str] = Field(default_factory=list)
    character_states: list[str] = Field(default_factory=list)
    location_description: str = Field(min_length=1)
    location_invariants: list[str] = Field(default_factory=list)
    props_and_ownership: list[str] = Field(default_factory=list)
    composition: str = Field(min_length=1)
    shot_size: str = Field(min_length=1)
    camera_angle: str = Field(min_length=1)
    subject_priority: list[str] = Field(default_factory=list)
    pose: str = Field(min_length=1)
    primary_action: str = Field(min_length=1)
    emotional_state: str = Field(min_length=1)
    continuity_assumptions: list[str] = Field(default_factory=list)
    required_source_evidence: list[UUID] = Field(default_factory=list)
    positive_constraints: list[str] = Field(default_factory=list)
    negative_constraints: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ImagePromptPackage(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    visual_intent: VisualIntent
    prompt: str = Field(min_length=1)
    prompt_compiler_version: str
    template_version: str
    references: list[ImageReferenceBinding] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_parameters: dict[str, Any] = Field(default_factory=dict)


class ImageProviderRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    application_idempotency_key: str = Field(min_length=1, max_length=255)
    project_id: UUID
    image_generation_run_id: UUID
    storyboard_id: UUID
    storyboard_version: int = Field(gt=0)
    shot_id: UUID
    shot_sequence: int = Field(ge=0)
    keyframe_role: KeyframeRole
    compiled_prompt: str = Field(min_length=1)
    references: list[ImageReferenceBinding] = Field(default_factory=list)
    model: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    quality: ImageQuality = ImageQuality.MEDIUM
    output_format: ImageFormat = ImageFormat.PNG
    background: Literal["opaque", "transparent"] = "opaque"
    provider_options: dict[str, Any] = Field(default_factory=dict)
    trace_context: dict[str, str] = Field(default_factory=dict)
    attempt_number: int = Field(gt=0)
    provider_configuration_version: str


class ImageProviderResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    model_snapshot: str | None = None
    requested_at: datetime
    provider_request_id: str | None = None
    attempt_number: int = Field(gt=0)
    returned_image_count: int = Field(ge=0)
    output_format: ImageFormat
    declared_width: int | None = Field(default=None, gt=0)
    declared_height: int | None = Field(default=None, gt=0)
    usage: dict[str, int] = Field(default_factory=dict)
    response_metadata: dict[str, str | int | bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)
    application_idempotency_key: str
    provider_configuration_version: str
    # Ephemeral adapter output only: excluded from serialization and public schema.
    image_base64: SkipJsonSchema[str] = Field(exclude=True, repr=False)


class ImageValidationDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: str
    severity: Literal["error", "warning"]
    message: str


class ImageValidationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    actual_format: ImageFormat | None = None
    mime_type: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    aspect_ratio: float | None = Field(default=None, gt=0)
    color_mode: str | None = None
    has_alpha: bool = False
    byte_size: int = Field(ge=0)
    sha256: str | None = None
    diagnostics: list[ImageValidationDiagnostic] = Field(default_factory=list)


class GeneratedImageCandidate(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    generated_image_id: UUID
    asset_id: UUID
    shot_id: UUID
    keyframe_role: KeyframeRole
    selected: bool
    validation: ImageValidationReport


class ShotKeyframeResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    keyframe_role: KeyframeRole
    status: Literal["completed", "reused", "failed"]
    prompt_hash: str
    candidate: GeneratedImageCandidate | None = None
    error_code: str | None = None


class ImageGenerationRunRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    storyboard_id: UUID | None = None
    idempotency_key: str
    provider_configuration_version: str
    shot_id: UUID | None = None
    keyframe_role: KeyframeRole | None = None


class ImageGenerationRunResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    storyboard_id: UUID
    storyboard_version: int = Field(gt=0)
    requested_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    status: str


class ImageGenerationResult(ImageGenerationRunResult):
    items: list[ShotKeyframeResult] = Field(default_factory=list)
