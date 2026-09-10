from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from services.narration.alignment import FakeAligner, RecognizedWord, reconcile_alignment
from services.narration.fake_provider import FakeNarrationProvider
from services.narration.normalization import normalize_audio, probe_audio
from services.narration.pipeline import NarrationPipeline, canonical_hash
from services.narration.quality import QualityThresholds, validate_quality
from tests.storyboard_fixtures import build_fixture
from vidgen.contracts.narration import NarrationAlignment, NarrationProviderRequest
from vidgen.db.narration_repository import NarrationRepository
from vidgen.db.script_models import ScriptSegment


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


def test_alignment_rejects_reversed_or_negative_timestamps() -> None:
    """A reversed or negative word is a broken transcript, not a duration overshoot."""
    with pytest.raises(ValueError, match=r"reversed|negative"):
        reconcile_alignment("hello", [RecognizedWord("hello", 0.5, 0.4)], 1)
    with pytest.raises(ValueError, match=r"reversed|negative"):
        reconcile_alignment("hello", [RecognizedWord("hello", -0.1, 0.4)], 1)
    with pytest.raises(ValueError, match="reversal"):
        reconcile_alignment(
            "go now", [RecognizedWord("go", 0.2, 0.6), RecognizedWord("now", 0.4, 0.8)], 1
        )


def test_alignment_keeps_words_inside_the_duration_unchanged() -> None:
    recognized = [RecognizedWord("hello", 0.0, 0.5), RecognizedWord("world", 0.5, 1.0)]
    alignment = reconcile_alignment("Hello world.", recognized, 1.0)
    assert [(w.start_seconds, w.end_seconds) for w in alignment.timings] == [(0.0, 0.5), (0.5, 1.0)]
    assert alignment.coverage == 1
    assert alignment.omissions == []
    assert alignment.diagnostics == []


def test_alignment_clamps_a_word_that_ends_slightly_after_the_audio() -> None:
    """Whisper overshoots ffprobe by a few frames; the word is trimmed, not refused."""
    recognized = [RecognizedWord("hello", 0.0, 0.5), RecognizedWord("world", 0.5, 1.02)]
    alignment = reconcile_alignment("Hello world.", recognized, 1.0)
    assert [(w.start_seconds, w.end_seconds) for w in alignment.timings] == [(0.0, 0.5), (0.5, 1.0)]
    assert alignment.coverage == 1
    assert alignment.omissions == []
    assert alignment.diagnostics == ["clamped 1 word timestamp(s) to the measured duration"]


def test_alignment_drops_a_word_that_lies_entirely_beyond_the_audio() -> None:
    """Both bounds past the duration would clamp to zero length; the word is dropped instead."""
    recognized = [
        RecognizedWord("hello", 0.0, 0.5),
        RecognizedWord("world", 0.5, 1.0),
        RecognizedWord("again", 1.01, 1.2),
    ]
    alignment = reconcile_alignment("Hello world again.", recognized, 1.0)
    assert [w.word for w in alignment.timings] == ["Hello", "world"]
    assert alignment.diagnostics == ["dropped 1 zero-length word(s) at or beyond the duration"]
    # A word starting exactly at the boundary has no room either.
    boundary = [RecognizedWord("hello", 0.0, 1.0), RecognizedWord("world", 1.0, 1.3)]
    assert [w.word for w in reconcile_alignment("hello world", boundary, 1.0).timings] == ["hello"]


def test_alignment_still_reconciles_the_remaining_words_after_a_drop() -> None:
    """The dropped word becomes an omission; every kept word keeps its approved index."""
    recognized = [
        RecognizedWord("our", 0.0, 0.3),
        RecognizedWord("hero", 0.3, 0.7),
        RecognizedWord("wakes", 0.7, 1.05),
        RecognizedWord("up", 1.1, 1.3),
    ]
    alignment = reconcile_alignment("Our hero wakes up!", recognized, 1.0)
    assert [w.word_index for w in alignment.timings] == [0, 1, 2]
    assert [w.punctuation for w in alignment.timings] == ["", "", ""]
    assert alignment.timings[-1].end_seconds == 1.0
    assert alignment.omissions == ["up"]
    assert alignment.insertions == []
    assert alignment.coverage == pytest.approx(3 / 4)
    assert alignment == reconcile_alignment("Our hero wakes up!", recognized, 1.0)


def _normalized_fake_audio(tmp_path: Path, text: str) -> tuple[Path, float]:
    provider = FakeNarrationProvider()
    raw = tmp_path / "raw.wav"
    normalized = tmp_path / "normalized.wav"
    asyncio.run(provider.generate(request(text), raw))
    normalize_audio(raw, normalized)
    return normalized, probe_audio(normalized).duration_seconds


def test_low_alignment_coverage_is_a_warning_by_default(tmp_path: Path) -> None:
    """Whisper misses domain vocabulary on clear audio, so coverage alone never fails a take."""
    text = "The pharmacokinetics of acetazolamide are discussed at length today."
    path, duration = _normalized_fake_audio(tmp_path, text)
    alignment = NarrationAlignment(timings=[], coverage=0.6)
    report = validate_quality(path, text, duration, alignment)
    assert report.valid is True
    assert [(d.code, d.severity) for d in report.diagnostics] == [("alignment_coverage", "warning")]
    assert report.diagnostics[0].measured_value == 0.6
    assert report.diagnostics[0].threshold == 0.9


def test_a_project_may_make_alignment_coverage_fail_again(tmp_path: Path) -> None:
    text = "The pharmacokinetics of acetazolamide are discussed at length today."
    path, duration = _normalized_fake_audio(tmp_path, text)
    alignment = NarrationAlignment(timings=[], coverage=0.6)
    strict = validate_quality(
        path, text, duration, alignment, QualityThresholds(warn_only_codes=[])
    )
    assert strict.valid is False
    assert [(d.code, d.severity) for d in strict.diagnostics] == [("alignment_coverage", "error")]


def test_a_relaxed_coverage_threshold_records_nothing(tmp_path: Path) -> None:
    text = "The pharmacokinetics of acetazolamide are discussed at length today."
    path, duration = _normalized_fake_audio(tmp_path, text)
    alignment = NarrationAlignment(timings=[], coverage=0.6)
    relaxed = QualityThresholds(min_alignment_coverage=0.5, warn_only_codes=[])
    report = validate_quality(path, text, duration, alignment, relaxed)
    assert report.valid is True
    assert report.diagnostics == []


def test_a_warned_code_never_hides_a_real_error(tmp_path: Path) -> None:
    """Demoting coverage does not demote the speaking-rate gate beside it."""
    text = "The pharmacokinetics of acetazolamide are discussed at length today."
    path, duration = _normalized_fake_audio(tmp_path, text)
    alignment = NarrationAlignment(timings=[], coverage=0.6)
    slow = QualityThresholds(min_wpm=500, max_wpm=600)
    report = validate_quality(path, text, duration, alignment, slow)
    assert report.valid is False
    assert {(d.code, d.severity) for d in report.diagnostics} == {
        ("speaking_rate", "error"),
        ("alignment_coverage", "warning"),
    }


def test_quality_thresholds_refuse_an_unknown_warn_only_code() -> None:
    with pytest.raises(ValueError, match="unknown narration quality codes: not_a_code"):
        QualityThresholds(warn_only_codes=["not_a_code"])
    with pytest.raises(ValueError, match="min_wpm must be below max_wpm"):
        QualityThresholds(min_wpm=220, max_wpm=220)
    # Deterministic: the list is bound into every segment's generation identity.
    assert QualityThresholds(
        warn_only_codes=["speaking_rate", "clipping", "speaking_rate"]
    ).warn_only_codes == ["clipping", "speaking_rate"]


def test_quality_thresholds_hash_deterministically_into_the_identity() -> None:
    first = QualityThresholds(warn_only_codes=["speaking_rate", "clipping"])
    second = QualityThresholds(warn_only_codes=["clipping", "speaking_rate"])
    assert canonical_hash(first.model_dump(mode="json")) == canonical_hash(
        second.model_dump(mode="json")
    )
    assert canonical_hash(first.model_dump(mode="json")) != canonical_hash(
        QualityThresholds().model_dump(mode="json")
    )


def test_the_authoritative_script_refuses_any_empty_segment(tmp_path: Path) -> None:
    """The script stage clears empty beats; narration never voices one, PAUSE included."""
    fixture = build_fixture(tmp_path, database_name="empty.db")
    repo = NarrationRepository(fixture.session)
    fixture.session.add(
        ScriptSegment(
            script_id=fixture.script.id,
            sequence=len(fixture.script_segments),
            stable_segment_id=uuid4(),
            segment_type="PAUSE",
            speaker_kind="narrator",
            text="",
            content_hash="a" * 64,
            plot_beat_ids=[],
            source_scene_ids=[],
            estimated_duration_ms=750,
        )
    )
    fixture.session.commit()
    with pytest.raises(ValueError, match="contains empty segments"):
        repo.authoritative_script(fixture.project.id)
    fixture.session.rollback()
    fixture.session.delete(
        fixture.session.scalars(
            select(ScriptSegment).where(ScriptSegment.segment_type == "PAUSE")
        ).one()
    )
    fixture.script_segments[0].text = "   "
    fixture.session.commit()
    with pytest.raises(ValueError, match="contains empty segments"):
        repo.authoritative_script(fixture.project.id)
