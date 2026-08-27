"""Strict provider-neutral T19 continuity-reference contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract

Sha256 = str
Status = Literal["draft", "approved", "rejected", "stale"]


class EvidenceLink(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: UUID
    scene_id: UUID | None = None
    source_timestamp_ms: int | None = Field(default=None, ge=0)


class ContinuityAmbiguity(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    field: str
    alternatives: list[str] = Field(min_length=1)
    evidence: list[EvidenceLink] = Field(default_factory=list)


class Interval(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    start_sequence: int = Field(ge=0)
    end_sequence: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> Interval:
        if self.end_sequence is not None and self.end_sequence < self.start_sequence:
            raise ValueError("end_sequence must be at or after start_sequence")
        return self


class CharacterAppearanceState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    interval: Interval
    wardrobe: list[str] = Field(default_factory=list)
    hairstyle: str | None = None
    injuries: list[str] = Field(default_factory=list)
    dirt_or_damage: list[str] = Field(default_factory=list)
    carried_props: list[str] = Field(default_factory=list)
    prop_ownership: dict[str, str] = Field(default_factory=dict)
    disguise: str | None = None
    emotional_state: str | None = None
    action_state: str | None = None
    evidence: list[EvidenceLink] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    unresolved_conflicts: list[str] = Field(default_factory=list)


class LocationEnvironmentState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    interval: Interval
    time_of_day: str | None = None
    weather: str | None = None
    lighting: str | None = None
    damage: list[str] = Field(default_factory=list)
    crowd_state: str | None = None
    prop_placement: dict[str, str] = Field(default_factory=dict)
    door_window_state: list[str] = Field(default_factory=list)
    active_hazards: list[str] = Field(default_factory=list)
    evidence: list[EvidenceLink] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    conflicts: list[str] = Field(default_factory=list)


class CharacterIdentityBible(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID
    display_name: str
    anonymous_speaker_label: str | None = None
    aliases: list[str] = Field(default_factory=list)
    role: str | None = None
    stable_traits: dict[str, str | list[str] | None] = Field(default_factory=dict)
    evidence: list[EvidenceLink] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    ambiguities: list[ContinuityAmbiguity] = Field(default_factory=list)


class LocationIdentityBible(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    location_id: UUID
    display_name: str
    location_type: str | None = None
    stable_traits: dict[str, str | list[str] | None] = Field(default_factory=dict)
    evidence: list[EvidenceLink] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    ambiguities: list[ContinuityAmbiguity] = Field(default_factory=list)


class CharacterIdentityVersion(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    project_id: UUID
    episode_analysis_id: UUID
    character_id: UUID
    version: int = Field(gt=0)
    bible: CharacterIdentityBible
    configuration_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    identity_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    status: Status = "draft"
    created_at: datetime


class LocationIdentityVersion(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    project_id: UUID
    episode_analysis_id: UUID
    location_id: UUID
    version: int = Field(gt=0)
    bible: LocationIdentityBible
    configuration_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    identity_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    status: Status = "draft"
    created_at: datetime


class CandidateScores(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    evidence: float = Field(ge=0, le=1)
    visibility: float = Field(ge=0, le=1)
    sharpness: float = Field(ge=0, le=1)
    exposure: float = Field(ge=0, le=1)
    obstruction: float = Field(ge=0, le=1)
    state_relevance: float = Field(ge=0, le=1)
    diversity: float = Field(ge=0, le=1)


class _ReferenceCandidate(StrictContract):
    asset_id: UUID
    source_scene_id: UUID
    source_timestamp_ms: int = Field(ge=0)
    interval: Interval | None = None
    scores: CandidateScores
    total_score: float = Field(ge=0, le=1)
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence: list[EvidenceLink] = Field(default_factory=list)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    selector_version: str
    created_at: datetime


class CharacterReferenceCandidate(_ReferenceCandidate):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID


class LocationReferenceCandidate(_ReferenceCandidate):
    schema_version: Literal["1.0"] = "1.0"
    location_id: UUID


class _ReferenceSet(StrictContract):
    id: UUID
    project_id: UUID
    identity_version_id: UUID
    reference_identity: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    ordered_asset_ids: list[UUID] = Field(min_length=1)
    primary_asset_id: UUID
    status: Status = "draft"
    created_at: datetime


class CharacterReferenceSet(_ReferenceSet):
    schema_version: Literal["1.0"] = "1.0"
    character_id: UUID


class LocationReferenceSet(_ReferenceSet):
    schema_version: Literal["1.0"] = "1.0"
    location_id: UUID


class ReferenceGenerationRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    identity_version_id: UUID
    entity_kind: Literal["character", "location"]
    ordered_source_asset_ids: list[UUID] = Field(min_length=1)
    provider: str
    model: str
    generation_parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)


class ReferenceValidationDiagnostic(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    code: str
    severity: Literal["error", "warning"]
    message: str


class ReferenceValidationReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    sha256: Sha256 | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    media_type: str | None = None
    diagnostics: list[ReferenceValidationDiagnostic] = Field(default_factory=list)


class ReferenceGenerationResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    reference_identity: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    asset_id: UUID | None = None
    provider_attempt_id: UUID | None = None
    provider_request_id: str | None = None
    reused: bool = False
    validation: ReferenceValidationReport


class CharacterStateSnapshot(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    shot_id: UUID
    character_id: UUID
    identity_version_id: UUID
    state: CharacterAppearanceState
    snapshot_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    resolver_version: str


class LocationStateSnapshot(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    shot_id: UUID
    location_id: UUID
    identity_version_id: UUID
    state: LocationEnvironmentState
    snapshot_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    resolver_version: str


class ReferenceBundleItem(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: UUID
    sha256: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    role: Literal[
        "character_identity", "character_state", "location_identity", "location_state", "prop"
    ]
    entity_id: UUID
    required: bool = True
    priority: int = Field(ge=0)


class ShotReferenceBundle(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    project_id: UUID
    storyboard_run_id: UUID
    shot_id: UUID
    shot_sequence: int = Field(ge=0)
    character_identity_version_ids: list[UUID] = Field(default_factory=list)
    character_state_snapshot_ids: list[UUID] = Field(default_factory=list)
    location_identity_version_id: UUID | None = None
    location_state_snapshot_id: UUID | None = None
    references: list[ReferenceBundleItem] = Field(default_factory=list)
    required_props: list[str] = Field(default_factory=list)
    continuity_warnings: list[str] = Field(default_factory=list)
    omitted_references: list[str] = Field(default_factory=list)
    provider_reference_limit: int = Field(ge=0)
    bundle_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    resolver_version: str
    created_at: datetime


class ReferenceApproval(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    project_id: UUID
    reference_set_id: UUID
    identity_version_id: UUID
    approving_principal: str
    upstream_lineage_hash: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: datetime


class ReferenceInvalidation(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    affected_shot_ids: list[UUID] = Field(default_factory=list)
    preserved_shot_ids: list[UUID] = Field(default_factory=list)
    stale_keyframe_ids: list[UUID] = Field(default_factory=list)
    stale_video_ids: list[UUID] = Field(default_factory=list)
    stale_render_ids: list[UUID] = Field(default_factory=list)
    estimated_cost_microusd: int = Field(default=0, ge=0)


class ContinuityReferenceResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    storyboard_run_id: UUID
    status: Literal["awaiting_approval", "complete", "failed", "cancelled"]
    character_version_ids: list[UUID] = Field(default_factory=list)
    location_version_ids: list[UUID] = Field(default_factory=list)
    bundle_ids: list[UUID] = Field(default_factory=list)
    invalidation: ReferenceInvalidation
