from __future__ import annotations

from services.subtitles.quality import score_subtitle
from vidgen.contracts.subtitles import SubtitleCandidate, SubtitleCue
from vidgen.contracts.transcription import TimeInterval


def test_forced_only_candidate_is_not_a_full_transcript() -> None:
    quality = score_subtitle(
        SubtitleCandidate(
            candidate_id="forced",
            source_type="embedded",
            provider="ffmpeg",
            language="en",
            subtitle_format="vtt",
            forced=True,
        ),
        [SubtitleCue(sequence=0, start_seconds=0, end_seconds=10, text="Foreign line")],
        duration_seconds=10,
        requested_languages=("en",),
    )
    assert not quality.passed
    assert "forced-only subtitle" in quality.reasons


def test_candidate_below_voiced_coverage_threshold_is_rejected() -> None:
    quality = score_subtitle(
        SubtitleCandidate(
            candidate_id="wrong-timing",
            source_type="embedded",
            provider="ffmpeg",
            language="en",
            subtitle_format="vtt",
        ),
        [
            SubtitleCue(
                sequence=sequence,
                start_seconds=0,
                end_seconds=0.1,
                text=f"Line {sequence}",
            )
            for sequence in range(20)
        ],
        duration_seconds=10,
        requested_languages=("en",),
        voiced=[TimeInterval(start_seconds=5, end_seconds=10)],
    )
    assert quality.score >= 0.55
    assert quality.voiced_coverage == 0
    assert not quality.passed
    assert "low voiced-audio coverage" in quality.reasons
