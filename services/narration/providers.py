"""Narration provider boundary and centralized capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from vidgen.contracts.narration import NarrationProviderRequest, NarrationProviderResult

OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
)
OPENAI_FORMATS = frozenset({"mp3", "opus", "aac", "flac", "wav", "pcm"})
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"


class NarrationProvider(Protocol):
    name: str

    async def generate(
        self, request: NarrationProviderRequest, destination: Path
    ) -> NarrationProviderResult: ...
