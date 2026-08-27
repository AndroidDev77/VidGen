from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class APISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDGEN_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen"
    blob_root: Path = Path(".local-data/blobs")
    upload_root: Path = Path(".local-data/uploads")
    signing_secret: str = "local-development-only-change-me"
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    allowed_video_types: tuple[str, ...] = ("video/mp4", "video/quicktime")
    openai_api_key: str | None = None
    transcription_model: str = "whisper-1"
    diarization_model: str = "gpt-4o-transcribe-diarize"
    analysis_model: str = "gpt-5.6"
    script_compressor_model: str = "gpt-5.6"
    script_writer_model: str = "gpt-5.6"
    script_editor_model: str = "gpt-5.6"
    storyboard_model: str = "gpt-5.6"
    image_model: str = "gpt-image-2-2026-04-21"
    runway_api_secret: str | None = None
    visual_capability_profile: str = "runway-gen4-turbo"
    opensubtitles_api_key: str | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None
    subtitle_languages: tuple[str, ...] = ("en",)
    subtitle_sync_enabled: bool = False
    temporal_allow_fake_providers: bool = False
    temporal_target_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    # T18 keeps Temporal out of API and frontend unit tests.
    temporal_use_fake_workflow_controller: bool = True
    # CORS stays disabled unless an explicit development allowlist is configured.
    cors_allowed_origins: tuple[str, ...] = ()

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("allowed_video_types", mode="before")
    @classmethod
    def parse_allowed_types(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("subtitle_languages", mode="before")
    @classmethod
    def parse_subtitle_languages(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip().lower() for item in value.split(",") if item.strip())
        return value


@lru_cache
def get_settings() -> APISettings:
    return APISettings()
