from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vidgen.contracts.generation import (
    GenerationCostEstimate,
    GenerationQuality,
    GenerationSettingsOrigin,
    ProjectGenerationSettings,
    ShotPacing,
)


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

    _exact_caps = field_validator("budget_warning_cap", "budget_hard_cap", mode="before")(
        exact_decimal_text
    )

    def generation_settings(self) -> ProjectGenerationSettings:
        return ProjectGenerationSettings(
            generation_quality=self.generation_quality,
            shot_pacing=self.shot_pacing,
            premium_fallback_allowed=self.premium_fallback_allowed,
            origin=GenerationSettingsOrigin.EXPLICIT,
        )


class SetGenerationSettingsRequest(BaseModel):
    """New generation settings for an existing project.

    Every field is required so a settings write is always the whole, explicit
    choice: there is no partial update that silently keeps a legacy default.
    """

    model_config = ConfigDict(extra="forbid")
    generation_quality: GenerationQuality
    shot_pacing: ShotPacing
    premium_fallback_allowed: bool = False

    def generation_settings(self) -> ProjectGenerationSettings:
        return ProjectGenerationSettings(
            generation_quality=self.generation_quality,
            shot_pacing=self.shot_pacing,
            premium_fallback_allowed=self.premium_fallback_allowed,
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


class ProjectStatusResponse(BaseModel):
    project_id: UUID
    status: str
    source_video_id: UUID | None
    source_asset_id: UUID | None
    upload_status: str | None
    error_code: str | None
