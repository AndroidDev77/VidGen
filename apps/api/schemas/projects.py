from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    target_duration_seconds: float = Field(default=300, gt=0, le=900)
    visual_style: str = Field(default="flat editorial cartoon", min_length=1)
    humor_intensity: int = Field(default=5, ge=0, le=10)


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


class ProjectStatusResponse(BaseModel):
    project_id: UUID
    status: str
    source_video_id: UUID | None
    source_asset_id: UUID | None
    upload_status: str | None
    error_code: str | None
