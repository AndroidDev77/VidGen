"""Deterministic comedy rubric scoring.

Real editorial judgment (is this actually funny?) belongs to the LLM provider. This
module supplies the same measurable-feature scoring the fake provider uses standing
in for that judgment, plus the shared approval-threshold logic every provider's
result is checked against.
"""

from __future__ import annotations

from collections import Counter

from services.script.validator import canonical_word_count
from vidgen.contracts.script import (
    ApprovalRecommendation,
    ComedyRubric,
    ComedyRubricScores,
    RecapScript,
)

RUBRIC_VERSION = "comedy-v1"
_WEIGHTS: dict[str, float] = {
    "plot_fidelity": 0.20,
    "clarity": 0.10,
    "joke_density": 0.10,
    "joke_variety": 0.10,
    "punchline_placement": 0.10,
    "spoken_rhythm": 0.10,
    "pacing": 0.10,
    "callback_quality": 0.08,
    "repetition": 0.06,
    "narratability": 0.06,
}
LONG_SEGMENT_WORDS = 40
LONG_DIALOGUE_WORDS = 30
DOMINANT_MECHANISM_RATIO = 0.6


def score_script(script: RecapScript, *, validation_error_count: int) -> ComedyRubricScores:
    segments = script.segments
    total = len(segments) or 1

    plot_fidelity = max(0.0, 100.0 - 15.0 * validation_error_count)
    clarity = max(0.0, 100.0 - 5.0 * validation_error_count)

    with_jokes = sum(1 for segment in segments if segment.joke_annotations or segment.visual_gag)
    joke_density = min(100.0, 100.0 * with_jokes / total)

    mechanisms = {
        annotation.joke_type for segment in segments for annotation in segment.joke_annotations
    }
    joke_variety = min(100.0, 100.0 * len(mechanisms) / 3)

    all_jokes = [annotation for segment in segments for annotation in segment.joke_annotations]
    with_punchline = sum(1 for joke in all_jokes if joke.punchline_span is not None)
    punchline_placement = 100.0 if not all_jokes else 100.0 * with_punchline / len(all_jokes)

    long_segments = sum(
        1 for segment in segments if canonical_word_count(segment.text) > LONG_SEGMENT_WORDS
    )
    spoken_rhythm = max(0.0, 100.0 - 10.0 * long_segments)

    if script.target_word_count:
        drift = abs(script.actual_word_count - script.target_word_count) / script.target_word_count
    else:
        drift = 0.0
    pacing = max(0.0, 100.0 - drift * 200.0)

    callback_quality = 100.0 if script.callbacks else 60.0

    mechanism_counts = Counter(joke.joke_type for joke in all_jokes)
    repetition = 100.0
    if mechanism_counts:
        _, dominant_count = mechanism_counts.most_common(1)[0]
        if dominant_count / max(len(all_jokes), 1) > DOMINANT_MECHANISM_RATIO:
            repetition = 80.0

    long_dialogue = sum(
        1
        for segment in segments
        if segment.type == "DIALOGUE" and canonical_word_count(segment.text) > LONG_DIALOGUE_WORDS
    )
    narratability = max(0.0, 100.0 - 10.0 * long_dialogue)

    scores = {
        "plot_fidelity": plot_fidelity,
        "clarity": clarity,
        "joke_density": joke_density,
        "joke_variety": joke_variety,
        "punchline_placement": punchline_placement,
        "spoken_rhythm": spoken_rhythm,
        "pacing": pacing,
        "callback_quality": callback_quality,
        "repetition": repetition,
        "narratability": narratability,
    }
    overall = sum(scores[key] * weight for key, weight in _WEIGHTS.items())
    return ComedyRubricScores(
        plot_fidelity=plot_fidelity,
        clarity=clarity,
        joke_density=joke_density,
        joke_variety=joke_variety,
        punchline_placement=punchline_placement,
        spoken_rhythm=spoken_rhythm,
        pacing=pacing,
        callback_quality=callback_quality,
        repetition=repetition,
        narratability=narratability,
        overall=round(overall, 2),
    )


def approval_recommendation(
    scores: ComedyRubricScores,
    rubric: ComedyRubric,
    *,
    mandatory_coverage_ratio: float,
    word_count_within_target: bool,
    validation_valid: bool,
) -> ApprovalRecommendation:
    if (
        scores.overall >= rubric.approval_overall_min
        and scores.plot_fidelity >= rubric.approval_plot_fidelity_min
        and mandatory_coverage_ratio >= 1.0
        and word_count_within_target
        and validation_valid
    ):
        return "approve"
    if scores.overall < 40 or not validation_valid:
        return "reject" if scores.overall < 40 else "revise"
    return "revise"


def default_rubric() -> ComedyRubric:
    return ComedyRubric(
        rubric_version=RUBRIC_VERSION,
        dimensions=list(_WEIGHTS),
        approval_overall_min=85,
        approval_plot_fidelity_min=92,
    )
