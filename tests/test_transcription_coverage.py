from __future__ import annotations

import pytest

from services.transcription.coverage import calculate_coverage
from vidgen.contracts.transcription import TimeInterval, TranscriptWord


def test_tolerated_word_gaps_count_as_covered() -> None:
    coverage = calculate_coverage(
        [TimeInterval(start_seconds=0, end_seconds=1)],
        [
            TranscriptWord(text="hello", start_seconds=0, end_seconds=0.4),
            TranscriptWord(text="world", start_seconds=0.6, end_seconds=1),
        ],
        gap_tolerance_seconds=0.25,
    )
    assert coverage.passed
    assert coverage.ratio == 1
    assert coverage.uncovered_intervals == []


def test_gap_above_tolerance_remains_uncovered() -> None:
    coverage = calculate_coverage(
        [TimeInterval(start_seconds=0, end_seconds=1)],
        [
            TranscriptWord(text="hello", start_seconds=0, end_seconds=0.2),
            TranscriptWord(text="world", start_seconds=0.8, end_seconds=1),
        ],
        gap_tolerance_seconds=0.25,
    )
    assert not coverage.passed
    assert coverage.ratio == pytest.approx(0.4)
    assert len(coverage.uncovered_intervals) == 1
