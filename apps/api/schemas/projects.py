from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.progress.engine import ProgressState
from vidgen.contracts.episode_analysis import WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.generation import (
    GenerationCostEstimate,
    GenerationQuality,
    GenerationSettingsOrigin,
    ProjectGenerationSettings,
    ShotPacing,
)
from vidgen.contracts.narration import (
    NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES,
    NarrationQualityThresholdOverrides,
    NarrationQualityThresholds,
)
from vidgen.contracts.script import SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES


def exact_decimal_text(value: object) -> object:
    """Accept only a budget amount that is still exact by the time it arrives.

    A JSON float has already lost the amount the owner typed, so it is refused
    here rather than stored as an approximation of a spend limit. Integers and
    ``Decimal`` are exact and are rendered as text; the amount itself is
    validated by ``services.costs.project_budget``.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('provide the amount as an exact decimal string, for example "25.00"')
    if isinstance(value, int | Decimal):
        return str(value)
    return value


def known_script_warn_only_codes(value: object) -> object:
    """The same check against the compression validator's own vocabulary."""
    if not isinstance(value, list):
        return value
    unknown = sorted(
        item for item in value if item not in SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
    )
    if unknown:
        raise ValueError(
            f"unknown validation codes: {', '.join(str(item) for item in unknown)}; "
            f"expected any of {', '.join(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES)}"
        )
    return value


def known_narration_quality_codes(value: object) -> object:
    """The same check against the T12 narration quality gate's vocabulary."""
    if not isinstance(value, list):
        return value
    unknown = sorted(
        item for item in value if item not in NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES
    )
    if unknown:
        raise ValueError(
            f"unknown narration quality codes: {', '.join(str(item) for item in unknown)}; "
            f"expected any of {', '.join(NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES)}"
        )
    return value


def known_warn_only_codes(value: object) -> object:
    """Mirror the contract's bounded vocabulary at the request boundary.

    ``ProjectGenerationSettings`` refuses an unknown code, so without this the
    request would parse and then fail inside the route as a 500 instead of
    telling the caller which code is wrong.
    """
    if not isinstance(value, list):
        return value
    unknown = sorted(item for item in value if item not in WARN_ONLY_ELIGIBLE_VALIDATION_CODES)
    if unknown:
        raise ValueError(
            f"unknown validation codes: {', '.join(str(item) for item in unknown)}; "
            f"expected any of {', '.join(WARN_ONLY_ELIGIBLE_VALIDATION_CODES)}"
        )
    return value


class CreateProjectRequest(BaseModel):
    """A new project, optionally with its narration voice already chosen.

    The voice is optional here and mandatory before the workflow starts. A
    project created without one is not broken - it simply cannot start until a
    voice is selected, and the setup screen and the start endpoint both say so.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    target_duration_seconds: float = Field(default=300, gt=0, le=900)
    visual_style: str = Field(default="flat editorial cartoon", min_length=1)
    humor_intensity: int = Field(default=5, ge=0, le=10)
    #: An existing profile, or a catalog option listed for this deployment.
    voice_profile_id: UUID | None = None
    #: An externally provisioned voice, named by its configured provider.
    voice_provider: str | None = Field(default=None, min_length=1, max_length=64)
    voice_provider_voice_id: str | None = Field(default=None, min_length=1, max_length=255)
    #: The project's T23 spend caps, in USD, as exact decimal strings. The
    #: warning cap is where the ledger starts flagging spend; the hard cap is
    #: the ceiling every paid activity reserves against. Both default to zero,
    #: which is a complete budget for a fake-provider run and is refused on a
    #: deployment that has a paid provider credential configured.
    budget_warning_cap: str = "0"
    budget_hard_cap: str = "0"
    #: Which Runway model tier the project may spend on. ``economy`` is Gen-4
    #: Turbo everywhere, ``balanced`` adds Gen-4.5 for hero shots and bounded
    #: quality repairs, ``premium`` is Gen-4.5 wherever compatible.
    generation_quality: GenerationQuality = GenerationQuality.BALANCED
    #: How long an edited shot should typically run: ``relaxed`` makes fewer,
    #: longer shots; ``fast`` makes more, shorter ones.
    shot_pacing: ShotPacing = ShotPacing.NORMAL
    #: Premium only: animate with Gen-4 Turbo instead of refusing when Gen-4.5
    #: cannot generate or the budget cannot afford a shot.
    premium_fallback_allowed: bool = False
    #: Per-project override of the scene-cut sensitivity used during media
    #: processing. Leave unset to use the deployment's global default.
    scene_detection_threshold: float | None = Field(default=None, gt=0, lt=1)
    #: Per-project override of the episode-analysis validation codes reported
    #: as warnings instead of failing the run. Leave unset to use the
    #: deployment's global default.
    warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: The same, for T11 plot compression.
    script_warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: Per-project override of the T12 narration quality codes recorded as
    #: warnings instead of failing the attempt. Leave unset to use the
    #: deployment's global default.
    narration_warn_only_quality_codes: list[str] | None = Field(default=None, max_length=16)
    #: Per-project override of the T12 narration quality limits; every unset
    #: limit keeps the deployment default. Leave unset for no override at all.
    narration_quality_thresholds: NarrationQualityThresholdOverrides | None = None

    _known_codes = field_validator("warn_only_validation_codes")(known_warn_only_codes)
    _known_script_codes = field_validator("script_warn_only_validation_codes")(
        known_script_warn_only_codes
    )
    _known_quality_codes = field_validator("narration_warn_only_quality_codes")(
        known_narration_quality_codes
    )

    _exact_caps = field_validator("budget_warning_cap", "budget_hard_cap", mode="before")(
        exact_decimal_text
    )

    def generation_settings(self) -> ProjectGenerationSettings:
        return ProjectGenerationSettings(
            generation_quality=self.generation_quality,
            shot_pacing=self.shot_pacing,
            premium_fallback_allowed=self.premium_fallback_allowed,
            scene_detection_threshold=self.scene_detection_threshold,
            warn_only_validation_codes=self.warn_only_validation_codes,
            script_warn_only_validation_codes=self.script_warn_only_validation_codes,
            narration_warn_only_quality_codes=self.narration_warn_only_quality_codes,
            narration_quality_thresholds=self.narration_quality_thresholds,
            origin=GenerationSettingsOrigin.EXPLICIT,
        )


class SetGenerationSettingsRequest(BaseModel):
    """New generation settings for an existing project.

    Every field is required so a settings write is always the whole, explicit
    choice: there is no partial update that silently keeps a legacy default.
    ``scene_detection_threshold`` is the one exception - it is an optional
    override, and ``None`` explicitly means "use the deployment default".
    """

    model_config = ConfigDict(extra="forbid")
    generation_quality: GenerationQuality
    shot_pacing: ShotPacing
    premium_fallback_allowed: bool
    scene_detection_threshold: float | None = Field(default=None, gt=0, lt=1)
    #: The same kind of optional override: ``None`` means "use the deployment
    #: default", an empty list means "tolerate nothing; every code fails".
    warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: The same, for T11 plot compression.
    script_warn_only_validation_codes: list[str] | None = Field(default=None, max_length=16)
    #: The same, for the T12 narration quality gate.
    narration_warn_only_quality_codes: list[str] | None = Field(default=None, max_length=16)
    #: Optional T12 quality-limit overrides; ``None`` (or an unset limit inside)
    #: means "use the deployment default".
    narration_quality_thresholds: NarrationQualityThresholdOverrides | None = None

    _known_codes = field_validator("warn_only_validation_codes")(known_warn_only_codes)
    _known_script_codes = field_validator("script_warn_only_validation_codes")(
        known_script_warn_only_codes
    )
    _known_quality_codes = field_validator("narration_warn_only_quality_codes")(
        known_narration_quality_codes
    )

    def generation_settings(self) -> ProjectGenerationSettings:
        return ProjectGenerationSettings(
            generation_quality=self.generation_quality,
            shot_pacing=self.shot_pacing,
            premium_fallback_allowed=self.premium_fallback_allowed,
            scene_detection_threshold=self.scene_detection_threshold,
            warn_only_validation_codes=self.warn_only_validation_codes,
            script_warn_only_validation_codes=self.script_warn_only_validation_codes,
            narration_warn_only_quality_codes=self.narration_warn_only_quality_codes,
            narration_quality_thresholds=self.narration_quality_thresholds,
            origin=GenerationSettingsOrigin.EXPLICIT,
        )


class GenerationSettingsResponse(BaseModel):
    """The project's resolved generation settings and their provenance."""

    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    settings: ProjectGenerationSettings
    #: The bounded identity Temporal binds; it changes whenever a material
    #: setting, policy version or provider capability changes.
    generation_policy_identity: str = Field(min_length=1, max_length=160)
    #: Whether a workflow run currently binds these settings. A change after
    #: that point applies to the next generation run, never to the running one.
    workflow_started: bool
    estimate: GenerationCostEstimate
    #: The scene-cut sensitivity actually used for media processing: the
    #: project's override from ``settings``, or the deployment default when
    #: ``settings.scene_detection_threshold`` is unset.
    effective_scene_detection_threshold: float = Field(gt=0, lt=1)
    #: The episode-analysis validation codes actually demoted to warnings: the
    #: project's override from ``settings``, or the deployment default when
    #: ``settings.warn_only_validation_codes`` is unset.
    effective_warn_only_validation_codes: list[str]
    #: Every code a project may choose to treat as a warning, for the UI.
    available_warn_only_validation_codes: list[str]
    #: The same three fields for the T11 compression validator.
    effective_script_warn_only_validation_codes: list[str]
    available_script_warn_only_validation_codes: list[str]
    #: The T12 narration quality gate actually in effect: every limit and the
    #: warn-only set, after the project's overrides are applied to the
    #: deployment defaults.
    effective_narration_quality_thresholds: NarrationQualityThresholds
    #: The narration quality codes actually demoted to warnings, and every
    #: code a project may choose to demote, for the UI.
    effective_narration_warn_only_quality_codes: list[str]
    available_narration_warn_only_quality_codes: list[str]


class GenerationEstimateRequest(BaseModel):
    """What the pre-creation estimate needs: a length and a pacing preset."""

    model_config = ConfigDict(extra="forbid")
    target_duration_seconds: float = Field(default=300, gt=0, le=900)
    shot_pacing: ShotPacing = ShotPacing.NORMAL


class SetProjectBudgetRequest(BaseModel):
    """New caps for an existing project.

    The same two exact decimal strings project creation takes, so a project that
    predates budgets - or one created with a zero cap for a fake run - can be
    funded without being recreated.
    """

    model_config = ConfigDict(extra="forbid")
    budget_warning_cap: str = "0"
    budget_hard_cap: str = "0"

    _exact_caps = field_validator("budget_warning_cap", "budget_hard_cap", mode="before")(
        exact_decimal_text
    )


class ProjectBudgetResponse(BaseModel):
    """The project's caps and the ledger totals recorded against them."""

    project_id: UUID
    warning_cap: str
    hard_cap: str
    currency: str
    policy_version: str
    reserved_amount: str
    committed_amount: str
    released_amount: str
    row_version: int = Field(ge=1)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    status: str
    target_duration_seconds: float
    visual_style: str
    humor_intensity: int
    created_at: datetime
    updated_at: datetime
    #: The project's selected narration voice, when it has one. A project
    #: without one cannot start its workflow, so the UI needs to see the
    #: difference without a second request.
    voice_profile_id: UUID | None = None
    #: The resolved generation settings. A project created before they existed
    #: resolves to ``economy`` and ``normal`` deterministically.
    generation_quality: GenerationQuality = GenerationQuality.BALANCED
    shot_pacing: ShotPacing = ShotPacing.NORMAL
    premium_fallback_allowed: bool = False


class ProjectListItemResponse(ProjectResponse):
    """The project-list row: everything the T18 list view renders.

    Costs stay exact decimal strings so no currency value is ever rounded by a
    binary float on the way to the browser.
    """

    current_stage: str | None = None
    progress_percentage: float | None = None
    committed_cost_amount: str | None = None
    hard_cap_amount: str | None = None
    has_failures: bool = False
    latest_failure_stage: str | None = None
    latest_failure_code: str | None = None
    row_version: int = Field(ge=1)


class StageProgressResponse(BaseModel):
    """Where the project's current stage is, read from its durable checkpoints.

    The dashboard polls this while a stage runs, so every field is something
    it can show directly: ``state`` drives the bar's tone and whether to keep
    polling, ``label`` is the heading, the counts and message sit beside the
    bar, and ``updated_at`` tells the owner the figures are current.
    """

    #: A stable stage id, the timeline's ``PipelineStage`` value where one exists.
    stage: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: ProgressState
    #: The stage-specific phase, e.g. ``"scene_analysis"`` or ``"animating"``.
    phase: str = Field(min_length=1)
    completed_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    #: Singular noun for one counted unit, e.g. ``"scene"``.
    unit: str = Field(min_length=1)
    #: What the counts count, e.g. ``"scenes analyzed"``.
    count_label: str
    percentage: float = Field(ge=0, le=100)
    message: str = Field(min_length=1)
    error_code: str | None = None
    updated_at: datetime | None = None


class ProjectStatusResponse(BaseModel):
    project_id: UUID
    status: str
    source_video_id: UUID | None
    source_asset_id: UUID | None
    upload_status: str | None
    error_code: str | None
    #: The stage that moved most recently; ``None`` until the workflow has
    #: started any stage that reports progress.
    stage_progress: StageProgressResponse | None = None
