"""Common contract types and validation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NonNegativeSeconds = Annotated[float, Field(ge=0)]
PositiveSeconds = Annotated[float, Field(gt=0)]
Score = Annotated[float, Field(ge=0, le=1)]


class StrictContract(BaseModel):
    """Base for contracts crossing a pipeline boundary."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AssetKind(StrEnum):
    SOURCE_VIDEO = "source_video"
    FRAME = "frame"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    RENDER = "render"
    THUMBNAIL = "thumbnail"
    JSON = "json"


class AssetRef(StrictContract):
    asset_id: UUID
    kind: AssetKind
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    uri: str
    media_type: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
