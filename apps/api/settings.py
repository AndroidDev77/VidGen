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
    opensubtitles_api_key: str | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None
    subtitle_languages: tuple[str, ...] = ("en",)
    subtitle_sync_enabled: bool = False
    temporal_allow_fake_providers: bool = False

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
