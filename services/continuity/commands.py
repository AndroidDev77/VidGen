"""Compact ID-only commands safe for Temporal history."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import StrictContract


class BuildReferencesCommand(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    episode_analysis_id: UUID
    storyboard_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    character_id: UUID | None = None
    location_id: UUID | None = None
    trace_context: dict[str, str] = Field(default_factory=dict)


class ApplyReferencesCommand(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    reference_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    trace_context: dict[str, str] = Field(default_factory=dict)
