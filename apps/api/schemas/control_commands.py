from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from vidgen.contracts.control_commands import ControlCommand, ProjectGenerationRun


class ControlCommandResponse(BaseModel):
    """One durable command, exactly as the control plane recorded it."""

    model_config = ConfigDict(extra="forbid")
    command: ControlCommand


class ControlCommandCollectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    items: list[ControlCommand]
    #: The project's generation lineage, newest last. Historical runs are kept.
    generation_runs: list[ProjectGenerationRun] = []
