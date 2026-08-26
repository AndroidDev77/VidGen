from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from services.narration.alignment import FakeAligner, RecognizedWord, reconcile_alignment
from services.narration.fake_provider import FakeNarrationProvider
from services.narration.normalization import normalize_audio, probe_audio
from services.narration.pipeline import NarrationPipeline, canonical_hash
from services.narration.quality import validate_quality
from vidgen.contracts.narration import NarrationProviderRequest


def request(text: str = "One repeated repeated joke.") -> NarrationProviderRequest:
    return NarrationProviderRequest(
        idempotency_key="stable",
        project_id=uuid4(),
        script_id=uuid4(),
        script_version=1,
        script_segment_id=uuid4(),
        segment_sequence=0,
        text=text,
        voice_profile_id=uuid4(),
        voice_profile_version=1,
        voice_id="cedar",
        model="fake-tts-1",
        output_format="wav",
        language="en",
        attempt_number=1,
    )


def test_generation_identity_is_canonical() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_fake_narration_and_normalization_are_deterministic(tmp_path: Path) -> None:
    provider = FakeNarrationProvider()
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    req = request()
    asyncio.run(provider.generate(req, first))
    asyncio.run(provider.generate(req, second))
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    normalized = tmp_path / "normalized.wav"
    normalize_audio(first, normalized)
    probe = probe_audio(normalized)
    assert (probe.codec, probe.sample_rate_hz, probe.channels) == ("pcm_s16le", 48_000, 1)
    alignment = FakeAligner().align(req.text, probe.duration_seconds)
    assert validate_quality(normalized, req.text, probe.duration_seconds, alignment).valid


def test_alignment_repeated_words_is_stable() -> None:
    recognized = [
        RecognizedWord("go", 0, 0.2),
        RecognizedWord("go", 0.2, 0.4),
        RecognizedWord("now", 0.4, 0.6),
    ]
    first = reconcile_alignment("Go, go now!", recognized, 0.6)
    assert first == reconcile_alignment("Go, go now!", recognized, 0.6)
    assert [word.word_index for word in first.timings] == [0, 1, 2]
    assert [word.punctuation for word in first.timings] == [",", "", "!"]


def test_retry_guidance_is_targeted() -> None:
    from types import SimpleNamespace

    previous = SimpleNamespace(
        quality_result={
            "diagnostics": [
                {"code": "alignment_coverage"},
                {"code": "speaking_rate"},
                {"code": "clipping"},
            ]
        }
    )
    guidance = NarrationPipeline._retry_instructions([previous])  # type: ignore[list-item]
    assert "Pronounce every approved word" in guidance
    assert "150 words per minute" in guidance
    assert "Avoid clipping" in guidance


def test_alignment_rejects_invalid_timestamps() -> None:
    with pytest.raises(ValueError, match=r"reversed|outside"):
        reconcile_alignment("hello", [RecognizedWord("hello", 0.5, 0.4)], 1)
    with pytest.raises(ValueError, match=r"reversed|outside"):
        reconcile_alignment("hello", [RecognizedWord("hello", 0, 2)], 1)


def test_contract_forbids_credentials() -> None:
    with pytest.raises(ValueError):
        NarrationProviderRequest(**{**request().model_dump(), "api_key": "secret"})
