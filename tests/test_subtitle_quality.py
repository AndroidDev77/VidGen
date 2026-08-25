from __future__ import annotations

from services.subtitles.quality import score_subtitle
from vidgen.contracts.subtitles import SubtitleCandidate, SubtitleCue


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
