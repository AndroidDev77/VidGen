from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vidgen.contracts.control_commands import VoiceProfileSelection


class VoiceProfileListResponse(BaseModel):
    """Every voice this project may select, and which one it currently uses."""

    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    items: list[VoiceProfileSelection]
    selected_voice_profile_id: UUID | None = None


class SelectVoiceProfileRequest(BaseModel):
    """Select an existing profile, or an externally provisioned provider voice.

    Deliberately has no credential field: a narration credential belongs to the
    deployment's configuration and is resolved by the worker, never sent by a
    browser and never stored on a profile.
    """

    model_config = ConfigDict(extra="forbid")
    voice_profile_id: UUID | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    provider_voice_id: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    language: str = Field(default="en", min_length=1, max_length=32)

    @model_validator(mode="after")
    def one_of(self) -> SelectVoiceProfileRequest:
        if self.voice_profile_id is None and not (self.provider and self.provider_voice_id):
            raise ValueError("provide voice_profile_id, or both provider and provider_voice_id")
        return self


class VoiceProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    profile: VoiceProfileSelection
