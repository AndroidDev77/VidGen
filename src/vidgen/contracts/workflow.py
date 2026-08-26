from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from vidgen.contracts.common import StrictContract


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    VALIDATION = "validation"
    QUOTA = "quota"
    PROVIDER = "provider"
    CANCELLED = "cancelled"


class WorkflowFailure(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    error_class: FailureClass
    code: str
    message: str
    retryable: bool
    details: dict[str, object] = Field(default_factory=dict)


class ProjectWorkflowInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    source_video_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class StageActivityInput(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    source_video_id: UUID
    stage: str
    idempotency_key: str = Field(min_length=1, max_length=255)


class StageActivityResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: str
    entity_id: UUID | None = None
    asset_id: UUID | None = None
    reused: bool = False


class ProjectWorkflowState(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    status: str
    completed_stages: list[str] = Field(default_factory=list)
    cancelled: bool = False
    failure: WorkflowFailure | None = None
    updated_at: datetime | None = None

    @field_validator("updated_at")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("updated_at must be timezone-aware")
        return value
