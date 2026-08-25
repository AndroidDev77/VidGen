from __future__ import annotations

from vidgen.contracts.transcription import TimeInterval, TranscriptCoverage, TranscriptWord


def calculate_coverage(
    voiced: list[TimeInterval],
    words: list[TranscriptWord],
    *,
    minimum_ratio: float = 0.98,
    gap_tolerance_seconds: float = 0.5,
) -> TranscriptCoverage:
    if not 0 <= minimum_ratio <= 1:
        raise ValueError("minimum coverage ratio must be between zero and one")
    voiced_union = _merge([(item.start_seconds, item.end_seconds) for item in voiced])
    word_union = _close_small_gaps(
        _merge([(word.start_seconds, word.end_seconds) for word in words]),
        gap_tolerance_seconds,
    )
    total = sum(end - start for start, end in voiced_union)
    covered = sum(
        max(0.0, min(v_end, w_end) - max(v_start, w_start))
        for v_start, v_end in voiced_union
        for w_start, w_end in word_union
    )
    uncovered = _subtract(voiced_union, word_union, gap_tolerance_seconds)
    ratio = 1.0 if total == 0 else min(1.0, covered / total)
    return TranscriptCoverage(
        voiced_seconds=total,
        covered_voiced_seconds=min(covered, total),
        ratio=ratio,
        passed=ratio >= minimum_ratio and not uncovered,
        uncovered_intervals=[
            TimeInterval(start_seconds=start, end_seconds=end) for start, end in uncovered
        ],
    )


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _close_small_gaps(
    intervals: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in intervals:
        if result and start - result[-1][1] <= tolerance:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _subtract(
    source: list[tuple[float, float]], covered: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    for start, end in source:
        cursor = start
        for covered_start, covered_end in covered:
            if covered_end <= cursor or covered_start >= end:
                continue
            if covered_start - cursor > tolerance:
                gaps.append((cursor, min(covered_start, end)))
            cursor = max(cursor, covered_end)
            if cursor >= end:
                break
        if end - cursor > tolerance:
            gaps.append((cursor, end))
    return gaps
