from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InitializeUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    expected_size: int = Field(gt=0)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    part_size: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    filename: str
    media_type: str
    expected_size: int
    expected_sha256: str
    part_size: int
    status: str
    completed_asset_id: UUID | None
    error_code: str | None


class UploadPartResponse(BaseModel):
    upload_id: UUID
    part_number: int
    byte_size: int
    sha256: str
    duplicate: bool


class CompleteUploadResponse(BaseModel):
    upload_id: UUID
    source_video_id: UUID
    asset_id: UUID
    sha256: str
    byte_size: int
    status: str


class DownloadURLResponse(BaseModel):
    asset_id: UUID
    url: str
    expires_in_seconds: int
