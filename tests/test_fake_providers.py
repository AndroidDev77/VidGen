from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidgen.contracts import EpisodeAnalysis
from vidgen.providers.fake import (
    FakeImageGenerator,
    FakeStructuredReasoner,
    FakeVideoGenerator,
    FakeVoiceGenerator,
)


@pytest.mark.asyncio
async def test_fake_image_is_deterministic() -> None:
    provider = FakeImageGenerator()
    first = await provider.generate(prompt="hero pose", seed=42, idempotency_key="shot-1")
    second = await provider.generate(prompt="hero pose", seed=42, idempotency_key="shot-1")
    assert first == second
    assert first.content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_fake_video_and_voice_are_deterministic() -> None:
    image = (await FakeImageGenerator().generate(prompt="x", idempotency_key="image-1")).content
    video_provider = FakeVideoGenerator()
    video = await video_provider.generate(
        image=image, prompt="slow zoom", duration_seconds=3.5, idempotency_key="video-1"
    )
    assert json.loads(video.content)["duration_seconds"] == 3.5
    voice_provider = FakeVoiceGenerator()
    first = await voice_provider.generate(
        text="hello tiny world", voice="fake", idempotency_key="v-1"
    )
    second = await voice_provider.generate(
        text="hello tiny world", voice="fake", idempotency_key="v-1"
    )
    assert first == second
    assert first.content[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_fake_reasoner_validates_contract() -> None:
    path = Path(__file__).parent / "fixtures" / "contracts" / "episode_analysis.valid.json"
    payload = json.loads(path.read_text())
    provider = FakeStructuredReasoner({EpisodeAnalysis: payload})
    result = await provider.generate(
        instructions="analyze",
        input_text="transcript",
        output_type=EpisodeAnalysis,
        idempotency_key="a-1",
    )
    assert result.title == "The Extremely Small Heist"
