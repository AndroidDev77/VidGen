"""Versioned project generation settings, routing decisions, and cost estimates.

Three product-level choices live here so every stage reads the same strict
values: the generation-quality mode that decides which Runway model a shot may
use, the shot-pacing preset that guides the Storyboard Director, and the bounded
routing decision that records why a model was selected for a provider attempt.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from vidgen.contracts.common import StrictContract
from vidgen.contracts.episode_analysis import WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.narration import (
    NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES,
    NarrationQualityThresholdOverrides,
)
from vidgen.contracts.script import SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.storyboard import STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES

GENERATION_SETTINGS_VERSION = "generation-settings/1"


class GenerationQuality(StrEnum):
    """Which Runway model tier a project may spend on.

    ``economy`` always animates with Gen-4 Turbo. ``balanced`` animates with
    Gen-4 Turbo and reserves Gen-4.5 for hero shots and bounded quality-repair
    escalations when the budget permits. ``premium`` animates every compatible
    shot with Gen-4.5 and refuses the run rather than silently downgrading.
    """

    ECONOMY = "economy"
    BALANCED = "balanced"
    PREMIUM = "premium"


class ShotPacing(StrEnum):
    """How long an edited shot should typically run.

    A preset is a creative preference the Storyboard Director plans against;
    the deterministic retimer remains the timing authority.
    """

    RELAXED = "relaxed"
    NORMAL = "normal"
    FAST = "fast"


class GenerationSettingsOrigin(StrEnum):
    #: The owner chose the values, or they were written explicitly at creation.
    EXPLICIT = "explicit"
    #: A project that predates these settings. It keeps the behaviour it had:
    #: Gen-4 Turbo everywhere and the retimer's original shot bounds.
    LEGACY_DEFAULT = "legacy_default"


class ProjectGenerationSettings(StrictContract):
    """The project's persisted generation settings, always fully resolved."""

    schema_version: Literal["1.0"] = "1.0"
    settings_version: Literal["generation-settings/1"] = "generation-settings/1"
    generation_quality: GenerationQuality = GenerationQuality.BALANCED
    shot_pacing: ShotPacing = ShotPacing.NORMAL
    #: Premium mode refuses a shot Gen-4.5 cannot generate, or cannot afford,
    #: unless this is set. Balanced and economy never consult it.
    premium_fallback_allowed: bool = False
    #: Per-project override of the scene-cut sensitivity used during media
    #: processing. ``None`` means the project has no override and uses the
    #: deployment's global ``scene_detection_threshold`` setting.
    scene_detection_threshold: float | None = Field(default=None, gt=0, lt=1)
    #: Per-project override of the episode-analysis validation codes that are
    #: reported as warnings instead of failing the run. ``None`` means the
    #: project has no override and uses the deployment's global
    #: ``warn_only_validation_codes`` setting.
    warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: The same, for the T11 compression validator. ``None`` means the project
    #: has no override and uses the deployment's global
    #: ``script_warn_only_validation_codes`` setting.
    script_warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: Per-project override of the T12 narration quality codes recorded as
    #: warnings instead of failing the attempt. ``None`` means the project has
    #: no override and uses the deployment's global
    #: ``narration_warn_only_quality_codes`` setting.
    narration_warn_only_quality_codes: list[str] | None = Field(default=None, max_length=16)
    #: Per-project override of the T12 narration quality limits. ``None``
    #: means the project has no override; inside it, every unset limit keeps
    #: the deployment default.
    narration_quality_thresholds: NarrationQualityThresholdOverrides | None = None
    #: The same, for the T13 storyboard validator. ``None`` means the project
    #: has no override and uses the deployment's global
    #: ``storyboard_warn_only_validation_codes`` setting.
    storyboard_warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    origin: GenerationSettingsOrigin = GenerationSettingsOrigin.EXPLICIT

    @field_validator("warn_only_validation_codes")
    @classmethod
    def validate_warn_only_codes(cls, value: list[str] | None) -> list[str] | None:
        """Only a code the validator can actually demote may be listed.

        An unknown code would be stored, shown in the UI and silently never
        match anything, so it is refused at the boundary instead.
        """
        if value is None:
            return None
        unknown = sorted(set(value) - set(WARN_ONLY_ELIGIBLE_VALIDATION_CODES))
        if unknown:
            raise ValueError(
                f"unknown validation codes: {', '.join(unknown)}; "
                f"expected any of {', '.join(WARN_ONLY_ELIGIBLE_VALIDATION_CODES)}"
            )
        # Deterministic and duplicate-free: these values are bound into stored
        # settings and compared to decide whether a settings write changed.
        return sorted(set(value))

    @field_validator("script_warn_only_validation_codes")
    @classmethod
    def validate_script_warn_only_codes(cls, value: list[str] | None) -> list[str] | None:
        """The compression validator's own vocabulary; see the check above."""
        if value is None:
            return None
        unknown = sorted(set(value) - set(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES))
        if unknown:
            raise ValueError(
                f"unknown validation codes: {', '.join(unknown)}; "
                f"expected any of {', '.join(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES)}"
            )
        return sorted(set(value))

    @field_validator("storyboard_warn_only_validation_codes")
    @classmethod
    def validate_storyboard_warn_only_codes(cls, value: list[str] | None) -> list[str] | None:
        """The storyboard validator's own vocabulary; see the check above."""
        if value is None:
            return None
        unknown = sorted(set(value) - set(STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES))
        if unknown:
            raise ValueError(
                f"unknown validation codes: {', '.join(unknown)}; "
                f"expected any of {', '.join(STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES)}"
            )
        return sorted(set(value))

    @field_validator("narration_warn_only_quality_codes")
    @classmethod
    def validate_narration_warn_only_quality_codes(
        cls, value: list[str] | None
    ) -> list[str] | None:
        """The narration quality gate's own vocabulary; see the check above."""
        if value is None:
            return None
        unknown = sorted(set(value) - set(NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES))
        if unknown:
            raise ValueError(
                f"unknown narration quality codes: {', '.join(unknown)}; "
                f"expected any of {', '.join(NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES)}"
            )
        return sorted(set(value))


class RoutingReasonCode(StrEnum):
    """The bounded vocabulary of reasons a model was selected."""

    ECONOMY_TURBO = "economy_turbo"
    EXPLICIT_MODEL = "explicit_model"
    BALANCED_DEFAULT_TURBO = "balanced_default_turbo"
    BALANCED_HERO_PREMIUM = "balanced_hero_premium"
    BALANCED_HERO_BUDGET_INSUFFICIENT = "balanced_hero_budget_insufficient"
    BALANCED_HERO_CAPABILITY_UNAVAILABLE = "balanced_hero_capability_unavailable"
    BALANCED_QUALITY_ESCALATION = "balanced_quality_escalation"
    BALANCED_ESCALATION_BUDGET_INSUFFICIENT = "balanced_escalation_budget_insufficient"
    BALANCED_ESCALATION_CAPABILITY_UNAVAILABLE = "balanced_escalation_capability_unavailable"
    BALANCED_ESCALATION_EXHAUSTED = "balanced_escalation_exhausted"
    PREMIUM_GEN4_5 = "premium_gen4_5"
    PREMIUM_FALLBACK_BUDGET = "premium_fallback_budget"
    PREMIUM_FALLBACK_CAPABILITY = "premium_fallback_capability"


class RoutingDecision(StrictContract):
    """Why one provider attempt used the model it used. Bounded and durable."""

    schema_version: Literal["1.0"] = "1.0"
    routing_policy_version: str = Field(min_length=1, max_length=64)
    quality_repair_policy_version: str = Field(min_length=1, max_length=64)
    quality_mode: GenerationQuality
    selected_model: str = Field(min_length=1, max_length=32)
    requested_model: str | None = Field(default=None, max_length=32)
    hero_shot: bool
    quality_escalation: bool = False
    escalation_attempt: int = Field(default=0, ge=0)
    attempt_number: int = Field(default=1, ge=1)
    prior_models: list[str] = Field(default_factory=list, max_length=16)
    reason_code: RoutingReasonCode
    reason: str = Field(min_length=1, max_length=255)
    requested_duration_seconds: float = Field(gt=0)
    generation_duration_seconds: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    estimated_cost: str = Field(min_length=1, max_length=32)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    budget_enforced: bool
    remaining_budget: str | None = Field(default=None, max_length=32)
    capability_profile_id: str = Field(min_length=1, max_length=128)
    capability_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[str] = Field(default_factory=list, max_length=8)


class GenerationCostEstimateMode(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    generation_quality: GenerationQuality
    primary_model: str = Field(min_length=1, max_length=32)
    hero_model: str = Field(min_length=1, max_length=32)
    estimated_low: str = Field(min_length=1, max_length=32)
    estimated_high: str = Field(min_length=1, max_length=32)
    #: Read as "this much more than economy"; economy reports zero.
    delta_from_economy_low: str = Field(min_length=1, max_length=32)
    delta_from_economy_high: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=255)


class GenerationCostEstimate(StrictContract):
    """A pre-workflow estimate of video-generation spend for each quality mode.

    It prices only Runway video generation, which is the spend the quality mode
    changes. Everything else a run buys - analysis, script, narration, keyframes,
    QA - is the same whichever mode is chosen.
    """

    schema_version: Literal["1.0"] = "1.0"
    estimate_version: Literal["generation-estimate/1"] = "generation-estimate/1"
    pricing_version: str = Field(min_length=1, max_length=64)
    capability_registry_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    target_duration_seconds: float = Field(gt=0)
    shot_pacing: ShotPacing
    estimated_shot_count_low: int = Field(ge=1)
    estimated_shot_count_high: int = Field(ge=1)
    generated_seconds_low: int = Field(ge=1)
    generated_seconds_high: int = Field(ge=1)
    hero_share: str = Field(min_length=1, max_length=16)
    retry_factor: str = Field(min_length=1, max_length=16)
    modes: list[GenerationCostEstimateMode] = Field(min_length=3, max_length=3)
    notes: list[str] = Field(default_factory=list, max_length=8)
