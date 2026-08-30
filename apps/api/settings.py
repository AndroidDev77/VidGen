from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from vidgen.storage.factory import SUPPORTED_BACKENDS


class APISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDGEN_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen"
    blob_root: Path = Path(".local-data/blobs")
    # "filesystem" keeps local development and every test suite free of the
    # Azure SDKs. A deployed environment sets "azure" and reaches the account
    # over a private endpoint with its managed identity; no key, connection
    # string or SAS token is ever read from configuration.
    blob_backend: str = "filesystem"
    blob_account_url: str | None = None
    blob_container: str = "assets"
    upload_root: Path = Path(".local-data/uploads")
    signing_secret: str = "local-development-only-change-me"
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    # Every comma-separated list setting is annotated ``NoDecode``. Without it
    # pydantic-settings treats a sequence field as complex and JSON-decodes the
    # environment value before any validator runs, so a documented value such as
    # ``VIDGEN_ALLOWED_VIDEO_TYPES=video/mp4,video/quicktime`` aborts start-up
    # with a JSONDecodeError. ``NoDecode`` hands the raw string to the
    # ``mode="before"`` validators below, which split it on commas.
    allowed_video_types: Annotated[tuple[str, ...], NoDecode] = ("video/mp4", "video/quicktime")
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
    # T22 final editorial QA reuses the same two-role policy over the assembled
    # recap: Luna evaluates on the inexpensive vision model, Terra adjudicates
    # only borderline findings on the stronger one. Both default to the model
    # this repository already has configured and verified.
    final_qa_first_pass_model: str = "gpt-5.6"
    final_qa_adjudicator_model: str = "gpt-5.6"
    #: Off by default: an unconfigured deployment must not silently skip the
    #: bounded second opinion and turn every borderline finding into a failure.
    final_qa_adjudication_enabled: bool = True
    runway_api_secret: str | None = None
    # T21 repair and fallback routing. The alternate provider is Google Veo; the
    # model is pinned by a versioned capability profile rather than named at
    # each call site. Set ``repair_alternate_provider`` to "none" to disable the
    # alternate-provider route entirely.
    # "none" by default: an unconfigured deployment keeps its same-provider
    # repairs and the free deterministic fallback instead of failing every
    # repair on a missing Google credential. Set to "veo" to enable the single
    # bounded alternate-provider attempt.
    repair_alternate_provider: str = "none"
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
    subtitle_languages: Annotated[tuple[str, ...], NoDecode] = ("en",)
    subtitle_sync_enabled: bool = False
    temporal_allow_fake_providers: bool = False
    temporal_target_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Temporal Cloud requires both. Off by default so a local
    # `temporal server start-dev` still connects in plaintext with no key.
    temporal_api_key: str | None = None
    temporal_tls_enabled: bool = False
    # T18 keeps Temporal out of API and frontend unit tests.
    # Off by default so an unconfigured deployment cannot silently report
    # workflows as running while nothing was started. Local development and the
    # test suites enable it explicitly.
    temporal_use_fake_workflow_controller: bool = False
    # --- T25 YouTube publication ---
    # The OAuth client ID is ordinary configuration: it appears in the
    # authorization URL the browser follows. The client secret and the token
    # encryption key are secrets and are resolved from Key Vault in a
    # deployment; neither has a default here.
    youtube_oauth_client_id: str | None = None
    youtube_oauth_client_secret: str | None = None
    #: Must be byte-identical to a redirect URI registered with Google.
    youtube_oauth_redirect_uri: str = "http://localhost:8000/api/v1/youtube/oauth:callback"
    #: Post-authorization targets the callback may send a browser to. Same-site
    #: paths only by default; anything else is refused.
    youtube_oauth_redirect_targets: Annotated[tuple[str, ...], NoDecode] = ("/",)
    #: Base64 AES-256 key for the credential envelope, and the version that
    #: names it. Rotation keeps retired keys decryptable until every ciphertext
    #: has been re-sealed.
    youtube_token_encryption_key: str | None = None
    youtube_token_encryption_key_version: str | None = None
    youtube_token_encryption_retired_keys: str | None = None
    #: Off by default. The development key lives in this repository, so it is
    #: never acceptable in a shared or deployed environment; local development
    #: and the test suites opt in explicitly.
    youtube_allow_dev_encryption_key: bool = False
    #: "fake" keeps local development and every test free of a YouTube project.
    youtube_provider: str = "fake"
    youtube_publisher_task_queue: str = "vidgen-publisher"
    youtube_upload_chunk_bytes: int = 8 * 1024 * 1024
    youtube_processing_timeout_seconds: int = 6 * 60 * 60
    # CORS stays disabled unless an explicit development allowlist is configured.
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("blob_backend")
    @classmethod
    def validate_blob_backend(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_BACKENDS:
            raise ValueError(f"blob_backend must be one of {SUPPORTED_BACKENDS}")
        return normalized

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

    @field_validator("youtube_oauth_redirect_targets", mode="before")
    @classmethod
    def parse_redirect_targets(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("youtube_provider")
    @classmethod
    def validate_youtube_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"fake", "youtube"}:
            raise ValueError("youtube_provider must be 'fake' or 'youtube'")
        return normalized

    @field_validator("youtube_upload_chunk_bytes")
    @classmethod
    def validate_chunk_bytes(cls, value: int) -> int:
        from services.publisher.youtube import normalize_chunk_bytes

        # Rounded to a legal 256 KiB multiple here rather than rejected, so a
        # misconfigured value never fails at byte zero of a large upload.
        return normalize_chunk_bytes(value)

    @field_validator("subtitle_languages", mode="before")
    @classmethod
    def parse_subtitle_languages(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip().lower() for item in value.split(",") if item.strip())
        return value


@lru_cache
def get_settings() -> APISettings:
    return APISettings()
