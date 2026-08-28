from __future__ import annotations

from decimal import Decimal
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
    # T20 visual QA. The design names two roles - Luna for the first pass and
    # Terra for adjudication - and the separation is a policy separation: an
    # independent attempt, a different prompt, and a higher confidence bar. Both
    # default to the model this repository already has configured and verified;
    # check the provider's current official documentation before changing one.
    visual_qa_first_pass_model: str = "gpt-5.6"
    visual_qa_adjudicator_model: str = "gpt-5.6"
    runway_api_secret: str | None = None
    # T21 repair and fallback routing. The alternate provider is Google Veo; the
    # model is pinned by a versioned capability profile rather than named at
    # each call site. Set ``repair_alternate_provider`` to "none" to disable the
    # alternate-provider route entirely.
    repair_alternate_provider: str = "veo"
    repair_max_same_provider_repairs: int = 2
    repair_allow_parallax_fallback: bool = True
    #: A configured per-shot repair spend limit, on top of the project budget.
    #: There is no hard-coded numeric default: leaving it unset means the
    #: project's T23 hard cap is the only money limit.
    repair_per_shot_cost_limit: Decimal | None = None
    veo_model: str | None = None
    google_cloud_project: str | None = None
    google_access_token: str | None = None
    veo_location: str = "us-central1"
    visual_capability_profile: str = "runway-gen4-turbo"
    # Provider names as bound into the T16 child-workflow identity.
    image_provider_name: str = "openai"
    video_provider_name: str = "runway"
    opensubtitles_api_key: str | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None
    subtitle_languages: tuple[str, ...] = ("en",)
    subtitle_sync_enabled: bool = False
    temporal_allow_fake_providers: bool = False
    temporal_target_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    # T18 keeps Temporal out of API and frontend unit tests.
    # Off by default so an unconfigured deployment cannot silently report
    # workflows as running while nothing was started. Local development and the
    # test suites enable it explicitly.
    temporal_use_fake_workflow_controller: bool = False
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
