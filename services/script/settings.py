"""Resolve T11 generation settings from project configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from vidgen.contracts.script import ChannelVoiceConfig, RecapMode
from vidgen.db.models import Project

MIN_DURATION_MS = 60_000
MAX_DURATION_MS = 1_800_000
MIN_WORDS = 100
MAX_WORDS = 4_000
DEFAULT_WORDS_PER_MINUTE = 150
SUPPORTED_RECAP_MODES: tuple[RecapMode, ...] = ("full_recap", "highlight_reel")


class ScriptSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScriptGenerationSettings:
    target_duration_ms: int
    target_words: int
    target_words_per_minute: int
    humor_intensity: float
    recap_mode: RecapMode
    required_beat_ids: list[UUID] = field(default_factory=list)
    excluded_topics: list[str] = field(default_factory=list)
    channel_voice: ChannelVoiceConfig = field(
        default_factory=lambda: ChannelVoiceConfig(narrator_persona="Wry, affectionate narrator")
    )
    prohibited_patterns: list[str] = field(default_factory=list)


def resolve_script_settings(
    project: Project, overrides: Mapping[str, object] | None = None
) -> ScriptGenerationSettings:
    raw = dict(project.settings.get("script", {})) if isinstance(project.settings, dict) else {}
    if not isinstance(raw, dict):
        raise ScriptSettingsError("project.settings.script must be an object")
    raw.update({key: value for key, value in (overrides or {}).items() if value is not None})

    target_duration_ms = int(
        raw.get("target_duration_ms") or round(project.target_duration_seconds * 1000)
    )
    if not MIN_DURATION_MS <= target_duration_ms <= MAX_DURATION_MS:
        raise ScriptSettingsError(
            f"target duration {target_duration_ms}ms is outside the configured bounds "
            f"[{MIN_DURATION_MS}, {MAX_DURATION_MS}]"
        )

    wpm = int(raw.get("target_words_per_minute", DEFAULT_WORDS_PER_MINUTE))
    if wpm <= 0:
        raise ScriptSettingsError("target narration rate must be positive")

    target_words = int(raw.get("target_words") or round(target_duration_ms / 60_000 * wpm))
    if not MIN_WORDS <= target_words <= MAX_WORDS:
        raise ScriptSettingsError(
            f"target word count {target_words} is outside the configured bounds "
            f"[{MIN_WORDS}, {MAX_WORDS}]"
        )

    raw_humor = raw.get("humor_intensity")
    humor_intensity = float(raw_humor) if raw_humor is not None else project.humor_intensity / 10.0
    if not 0.0 <= humor_intensity <= 1.0:
        raise ScriptSettingsError("humor intensity must be between 0 and 1")

    recap_mode = raw.get("recap_mode", "full_recap")
    if recap_mode not in SUPPORTED_RECAP_MODES:
        raise ScriptSettingsError(f"unsupported recap mode: {recap_mode}")

    try:
        required_beat_ids = [UUID(str(value)) for value in raw.get("required_beat_ids", [])]
    except ValueError as error:
        raise ScriptSettingsError(f"invalid required beat ID: {error}") from error

    excluded_topics = [str(topic) for topic in raw.get("excluded_topics", [])]
    channel_voice_raw = raw.get("channel_voice") or {
        "narrator_persona": "Wry, affectionate narrator"
    }
    channel_voice = ChannelVoiceConfig.model_validate(channel_voice_raw)
    prohibited_patterns = [str(pattern) for pattern in raw.get("prohibited_patterns", [])]

    return ScriptGenerationSettings(
        target_duration_ms=target_duration_ms,
        target_words=target_words,
        target_words_per_minute=wpm,
        humor_intensity=humor_intensity,
        recap_mode=recap_mode,
        required_beat_ids=required_beat_ids,
        excluded_topics=excluded_topics,
        channel_voice=channel_voice,
        prohibited_patterns=prohibited_patterns,
    )
