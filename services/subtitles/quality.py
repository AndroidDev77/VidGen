from __future__ import annotations

from vidgen.contracts.subtitles import SubtitleCandidate, SubtitleCue, SubtitleQuality
from vidgen.contracts.transcription import TimeInterval


def score_subtitle(
    candidate: SubtitleCandidate,
    cues: list[SubtitleCue],
    *,
    duration_seconds: float,
    requested_languages: tuple[str, ...],
    voiced: list[TimeInterval] | None = None,
    sync_offset_seconds: float | None = None,
    sync_correlation: float | None = None,
    minimum_score: float = 0.55,
    allow_forced: bool = False,
) -> SubtitleQuality:
    if duration_seconds <= 0:
        raise ValueError("video duration must be positive")
    cue_intervals = _merge(
        [(cue.start_seconds, min(cue.end_seconds, duration_seconds)) for cue in cues]
    )
    cue_seconds = sum(max(0.0, end - start) for start, end in cue_intervals)
    timeline_coverage = min(1.0, cue_seconds / duration_seconds)
    voiced_coverage = None
    if voiced:
        voiced_seconds = sum(item.end_seconds - item.start_seconds for item in voiced)
        covered = _intersection_seconds(
            [(item.start_seconds, item.end_seconds) for item in voiced], cue_intervals
        )
        voiced_coverage = min(1.0, covered / voiced_seconds) if voiced_seconds else None

    source_score = {"embedded": 0.30, "sidecar": 0.28, "provider": 0.22}[candidate.source_type]
    language_score = (
        0.20
        if candidate.language in requested_languages
        or (candidate.language in {"en", "eng"} and "en" in requested_languages)
        else 0.05
    )
    coverage_basis = (
        voiced_coverage if voiced_coverage is not None else min(1.0, timeline_coverage * 4)
    )
    coverage_score = coverage_basis * 0.30
    cue_score = min(1.0, len(cues) / 20) * 0.10
    sync_score = _sync_score(sync_offset_seconds, sync_correlation) * 0.10
    penalty = (0.15 if candidate.forced else 0.0) + (0.03 if candidate.hearing_impaired else 0)
    score = max(
        0.0,
        min(1.0, source_score + language_score + coverage_score + cue_score + sync_score - penalty),
    )
    reasons: list[str] = []
    if candidate.forced:
        reasons.append("forced-only subtitle")
    if not cues:
        reasons.append("no valid cues")
    if any(cue.end_seconds > duration_seconds + 2 for cue in cues):
        reasons.append("cue extends beyond source duration")
    if voiced_coverage is not None and voiced_coverage < 0.55:
        reasons.append("low voiced-audio coverage")
    passed = (
        score >= minimum_score
        and bool(cues)
        and (allow_forced or not candidate.forced)
        and (voiced_coverage is None or voiced_coverage >= 0.55)
        and not any(cue.end_seconds > duration_seconds + 2 for cue in cues)
    )
    return SubtitleQuality(
        candidate_id=candidate.candidate_id,
        score=score,
        cue_count=len(cues),
        timeline_coverage=timeline_coverage,
        voiced_coverage=voiced_coverage,
        sync_offset_seconds=sync_offset_seconds,
        sync_correlation=sync_correlation,
        passed=passed,
        reasons=reasons,
    )


def candidate_sort_key(
    candidate: SubtitleCandidate, requested_languages: tuple[str, ...]
) -> tuple[int, int, int, int, str]:
    language = int(
        candidate.language in requested_languages
        or (candidate.language in {"en", "eng"} and "en" in requested_languages)
    )
    source = {"embedded": 3, "sidecar": 2, "provider": 1}[candidate.source_type]
    return (
        language,
        source,
        int(not candidate.forced),
        candidate.download_count,
        candidate.candidate_id,
    )


def _sync_score(offset: float | None, correlation: float | None) -> float:
    if correlation is None:
        return 0.5
    correlation_score = max(0.0, min(1.0, (correlation - 2_500) / 297_500))
    offset_score = 1.0 if offset is None else max(0.0, 1 - abs(offset) / 30)
    return (correlation_score * 0.7) + (offset_score * 0.3)


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _intersection_seconds(
    left: list[tuple[float, float]], right: list[tuple[float, float]]
) -> float:
    total = 0.0
    for left_start, left_end in left:
        for right_start, right_end in right:
            total += max(0.0, min(left_end, right_end) - max(left_start, right_start))
    return total
