from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    row_version: int = Field(ge=1)


class ProjectStatusResponse(BaseModel):
    project_id: UUID
    status: str
    source_video_id: UUID | None
    source_asset_id: UUID | None
    upload_status: str | None
    error_code: str | None
