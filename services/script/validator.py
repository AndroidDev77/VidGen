"""Deterministic validation for compressed plot plans and recap scripts.

Providers never validate their own output. Every rule here is computed from
canonical text and cross-referenced against the authoritative T10 ``EpisodeAnalysis``
and the ``CompressedPlotPlan`` the script was written from; no LLM call is involved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

from services.script.compressor import structural_roles
from vidgen.contracts.episode_analysis import EpisodeAnalysis
from vidgen.contracts.script import (
    BeatCoverage,
    CompressedPlotPlan,
    PlotCompressionRequest,
    RecapScript,
    ScriptSegment,
    ScriptValidationError,
    ScriptValidationReport,
)

WORD_COUNT_TOLERANCE = 0.05
WORD_BUDGET_TOLERANCE = 0.02
DURATION_TOLERANCE = 0.10
HIGH_HUMOR_THRESHOLD = 0.7
MAX_EXPOSITION_INTERVAL_MS = 18_000
MAX_CONSECUTIVE_EXPOSITION = 2
DEFAULT_SIMILARITY_THRESHOLD = 0.5
NGRAM_SIZE = 6


def canonical_word_count(text: str) -> int:
    return len(text.split())


def _normalize(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s]", " ", text.casefold()).split()


def ngram_overlap_ratio(candidate: str, reference: str, n: int = NGRAM_SIZE) -> float:
    """Bounded n-gram overlap ratio in [0, 1] used for near-verbatim detection."""
    candidate_tokens = _normalize(candidate)
    reference_tokens = _normalize(reference)
    if len(candidate_tokens) < n or len(reference_tokens) < n:
        return 0.0
    candidate_grams = {
        tuple(candidate_tokens[i : i + n]) for i in range(len(candidate_tokens) - n + 1)
    }
    reference_grams = {
        tuple(reference_tokens[i : i + n]) for i in range(len(reference_tokens) - n + 1)
    }
    if not candidate_grams:
        return 0.0
    return len(candidate_grams & reference_grams) / len(candidate_grams)


def _error(
    errors: list[ScriptValidationError], code: str, path: str, value: object, explanation: str
) -> None:
    errors.append(
        ScriptValidationError(
            code=code, entity_path=path, invalid_value=str(value), explanation=explanation
        )
    )


def _all_reference_ids(analysis: EpisodeAnalysis) -> set[UUID]:
    ids: set[UUID] = {ref.reference_id for ref in analysis.source_references}
    for beat in analysis.plot_beats:
        ids |= {ref.reference_id for ref in beat.source_references}
    for scene in analysis.scenes:
        ids |= {ref.reference_id for ref in scene.source_references}
    return ids


def validate_compressed_plot_plan(
    plan: CompressedPlotPlan, *, analysis: EpisodeAnalysis, request: PlotCompressionRequest
) -> ScriptValidationReport:
    errors: list[ScriptValidationError] = []
    beats_by_id = {beat.plot_beat_id: beat for beat in analysis.plot_beats}
    selected_ids = [beat.plot_beat_id for beat in plan.selected_beats]
    omitted_ids = [beat.plot_beat_id for beat in plan.omitted_beats]

    if len(selected_ids) != len(set(selected_ids)):
        _error(
            errors, "DUPLICATE_ID", "selected_beats", selected_ids, "Selected beats must be unique"
        )
    if len(omitted_ids) != len(set(omitted_ids)):
        _error(errors, "DUPLICATE_ID", "omitted_beats", omitted_ids, "Omitted beats must be unique")

    for index, beat_id in enumerate(selected_ids):
        if beat_id not in beats_by_id:
            _error(
                errors,
                "UNKNOWN_BEAT",
                f"selected_beats.{index}",
                beat_id,
                "Selected beat must exist in the episode analysis",
            )
    for index, beat_id in enumerate(omitted_ids):
        if beat_id not in beats_by_id:
            _error(
                errors,
                "UNKNOWN_BEAT",
                f"omitted_beats.{index}",
                beat_id,
                "Omitted beat must exist in the episode analysis",
            )

    mandatory_ids = {beat.plot_beat_id for beat in analysis.plot_beats if beat.mandatory}
    missing_mandatory = mandatory_ids - set(selected_ids)
    for beat_id in missing_mandatory:
        _error(
            errors,
            "MANDATORY_BEAT_OMITTED",
            "selected_beats",
            beat_id,
            "A mandatory beat was omitted",
        )
    missing_required = set(request.required_beat_ids) - set(selected_ids)
    for beat_id in missing_required:
        _error(
            errors,
            "REQUIRED_BEAT_OMITTED",
            "selected_beats",
            beat_id,
            "A required beat was omitted",
        )

    roles = structural_roles(analysis.plot_beats)
    for beat_id, role in roles.items():
        if beat_id not in selected_ids:
            _error(
                errors,
                "STRUCTURAL_BEAT_OMITTED",
                "selected_beats",
                beat_id,
                f"The authoritative analysis has a '{role}' beat that must be retained",
            )

    for index, beat in enumerate(plan.selected_beats):
        source = beats_by_id.get(beat.plot_beat_id)
        if source is not None and beat.summary != source.summary:
            _error(
                errors,
                "UNSUPPORTED_BEAT_SUMMARY",
                f"selected_beats.{index}.summary",
                beat.summary,
                "Compression must not alter or embellish the source beat summary",
            )

    for index, omitted in enumerate(plan.omitted_beats):
        if not omitted.reason.strip():
            _error(
                errors,
                "OMISSION_WITHOUT_REASON",
                f"omitted_beats.{index}",
                omitted.plot_beat_id,
                "Every omission needs a reason",
            )

    valid_reference_ids = _all_reference_ids(analysis)
    for index, beat in enumerate(plan.selected_beats):
        for ref_index, reference in enumerate(beat.source_references):
            if reference.reference_id not in valid_reference_ids:
                _error(
                    errors,
                    "UNKNOWN_SOURCE_REFERENCE",
                    f"selected_beats.{index}.source_references.{ref_index}",
                    reference.reference_id,
                    "Source reference must resolve to the selected episode analysis",
                )

    beat_sequence = {beat.plot_beat_id: beat.sequence for beat in analysis.plot_beats}
    graph: dict[UUID, list[UUID]] = {beat_id: [] for beat_id in selected_ids}
    selected_set = set(selected_ids)
    connective_pairs = {
        (item.cause_beat_id, item.effect_beat_id) for item in plan.connective_explanations
    }
    for dependency in analysis.beat_dependencies:
        if dependency.cause_beat_id in selected_set and dependency.effect_beat_id in selected_set:
            graph.setdefault(dependency.cause_beat_id, []).append(dependency.effect_beat_id)
            if beat_sequence[dependency.cause_beat_id] >= beat_sequence[dependency.effect_beat_id]:
                _error(
                    errors,
                    "CAUSE_AFTER_EFFECT",
                    "selected_beats",
                    dependency.effect_beat_id,
                    "A selected cause must precede its selected effect",
                )
        elif (
            dependency.effect_beat_id in selected_set
            and dependency.cause_beat_id not in selected_set
        ):
            if (dependency.cause_beat_id, dependency.effect_beat_id) not in connective_pairs:
                _error(
                    errors,
                    "MISSING_CAUSAL_BRIDGE",
                    "selected_beats",
                    dependency.effect_beat_id,
                    "A selected beat's cause was omitted without a connective explanation",
                )

    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cycle = any(visit(child) for child in graph.get(node, []))
        visiting.discard(node)
        visited.add(node)
        return cycle

    if any(visit(node) for node in graph if node not in visited):
        _error(
            errors,
            "CYCLIC_BEAT_DEPENDENCY",
            "selected_beats",
            "cycle",
            "Selected beats must form a DAG",
        )

    total_words = sum(item.words for item in plan.word_budget.allocations)
    target = plan.word_budget.total_target_words
    if target > 0 and abs(total_words - target) / target > WORD_BUDGET_TOLERANCE:
        _error(
            errors,
            "WORD_BUDGET_OFF_TARGET",
            "word_budget",
            total_words,
            f"Per-beat word allocations must sum to the target within {WORD_BUDGET_TOLERANCE:.0%}",
        )

    total_duration = sum(item.estimated_duration_ms for item in plan.pacing_plan)
    if request.target_duration_ms > 0 and (
        abs(total_duration - request.target_duration_ms) / request.target_duration_ms
        > DURATION_TOLERANCE
    ):
        _error(
            errors,
            "PACING_OFF_TARGET",
            "pacing_plan",
            total_duration,
            f"Total estimated duration must fit the target within {DURATION_TOLERANCE:.0%}",
        )

    return ScriptValidationReport(valid=not errors, errors=errors)


def validate_recap_script(
    script: RecapScript,
    *,
    analysis: EpisodeAnalysis,
    plan: CompressedPlotPlan,
    prohibited_patterns: list[str] | None = None,
    transcript_texts: list[str] | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    previous_script: RecapScript | None = None,
    previous_coverage: Mapping[UUID, str] | None = None,
    allow_anonymous_speakers: bool = True,
) -> ScriptValidationReport:
    errors: list[ScriptValidationError] = []
    character_ids = {character.character_id for character in analysis.characters}
    scene_ids = {scene.scene_id for scene in analysis.scenes}
    plan_beat_ids = {beat.plot_beat_id for beat in plan.selected_beats}
    mandatory_beat_ids = {beat.plot_beat_id for beat in plan.selected_beats if beat.mandatory}
    valid_reference_ids = _all_reference_ids(analysis)
    segments_by_id = {segment.segment_id: segment for segment in script.segments}

    actual_words = sum(canonical_word_count(segment.text) for segment in script.segments)
    if actual_words != script.actual_word_count:
        _error(
            errors,
            "WORD_COUNT_MISMATCH",
            "actual_word_count",
            script.actual_word_count,
            "actual_word_count must equal the canonical word count of segment text",
        )
    if script.target_word_count > 0 and (
        abs(actual_words - script.target_word_count) / script.target_word_count
        > WORD_COUNT_TOLERANCE
    ):
        _error(
            errors,
            "WORD_COUNT_OUT_OF_RANGE",
            "actual_word_count",
            actual_words,
            f"Actual word count must be within {WORD_COUNT_TOLERANCE:.0%} of the target",
        )

    covered_by_beat: dict[UUID, list[UUID]] = {beat_id: [] for beat_id in plan_beat_ids}
    for segment in script.segments:
        for beat_id in segment.plot_beat_ids:
            if beat_id not in plan_beat_ids:
                _error(
                    errors,
                    "UNKNOWN_PLOT_BEAT_REFERENCE",
                    f"segments.{segment.segment_id}.plot_beat_ids",
                    beat_id,
                    "Segment references a plot beat outside the compressed plan",
                )
            else:
                covered_by_beat[beat_id].append(segment.segment_id)
        for scene_id in segment.source_scene_ids:
            if scene_id not in scene_ids:
                _error(
                    errors,
                    "UNKNOWN_SCENE_REFERENCE",
                    f"segments.{segment.segment_id}.source_scene_ids",
                    scene_id,
                    "Segment references a scene outside the episode analysis",
                )
        if (
            segment.speaker_kind == "character"
            and segment.speaker_character_id not in character_ids
        ):
            _error(
                errors,
                "UNKNOWN_SPEAKER",
                f"segments.{segment.segment_id}.speaker_character_id",
                segment.speaker_character_id,
                "Dialogue speaker must resolve to an episode analysis character",
            )
        if segment.speaker_kind == "anonymous" and not allow_anonymous_speakers:
            _error(
                errors,
                "ANONYMOUS_SPEAKER_NOT_PERMITTED",
                f"segments.{segment.segment_id}.anonymous_speaker_label",
                segment.anonymous_speaker_label,
                "Anonymous speakers are not permitted by configuration",
            )
        for annotation in segment.joke_annotations:
            for span_name, span in (
                ("setup_span", annotation.setup_span),
                ("punchline_span", annotation.punchline_span),
            ):
                if span is not None and span.end > len(segment.text):
                    _error(
                        errors,
                        "INVALID_JOKE_SPAN",
                        f"segments.{segment.segment_id}.{span_name}",
                        span.end,
                        "Joke character span must be valid for the segment text",
                    )
        if prohibited_patterns:
            lowered = segment.text.casefold()
            for pattern in prohibited_patterns:
                if pattern and pattern.casefold() in lowered:
                    _error(
                        errors,
                        "PROHIBITED_PATTERN",
                        f"segments.{segment.segment_id}.text",
                        pattern,
                        "Segment text matches a prohibited comedy pattern",
                    )
        if transcript_texts:
            worst = max(
                (ngram_overlap_ratio(segment.text, reference) for reference in transcript_texts),
                default=0.0,
            )
            if worst > similarity_threshold:
                _error(
                    errors,
                    "NEAR_VERBATIM_TRANSCRIPT",
                    f"segments.{segment.segment_id}.text",
                    round(worst, 3),
                    "Segment text is too close to the source transcript; paraphrase it",
                )

    for beat_id in plan_beat_ids:
        if not covered_by_beat.get(beat_id):
            code = (
                "MANDATORY_BEAT_NOT_COVERED"
                if beat_id in mandatory_beat_ids
                else "BEAT_NOT_COVERED"
            )
            _error(
                errors,
                code,
                "beat_coverage",
                beat_id,
                "Every selected beat must be covered by a segment",
            )

    for callback in script.callbacks:
        setup = segments_by_id.get(callback.setup_segment_id)
        payoff = segments_by_id.get(callback.payoff_segment_id)
        if setup is None:
            _error(
                errors,
                "UNKNOWN_CALLBACK_SEGMENT",
                "callbacks",
                callback.setup_segment_id,
                "Callback setup segment must resolve",
            )
        if payoff is None:
            _error(
                errors,
                "UNKNOWN_CALLBACK_SEGMENT",
                "callbacks",
                callback.payoff_segment_id,
                "Callback payoff segment must resolve",
            )
        if setup is not None and payoff is not None and payoff.sequence <= setup.sequence:
            _error(
                errors,
                "CALLBACK_PAYOFF_BEFORE_SETUP",
                "callbacks",
                callback.callback_id,
                "Callback payoff must occur after its setup",
            )

    for reference in script.source_refs:
        if reference.reference_id not in valid_reference_ids:
            _error(
                errors,
                "UNKNOWN_SOURCE_REFERENCE",
                "source_refs",
                reference.reference_id,
                "Source reference must resolve to the selected episode analysis",
            )

    ordered = sorted(script.segments, key=lambda segment: segment.sequence)
    streak = 0
    for segment in ordered:
        exposition_only = not segment.joke_annotations and not segment.visual_gag
        if script.humor_intensity >= HIGH_HUMOR_THRESHOLD:
            streak = streak + 1 if exposition_only else 0
            if streak > MAX_CONSECUTIVE_EXPOSITION:
                _error(
                    errors,
                    "TOO_MUCH_EXPOSITION",
                    f"segments.{segment.segment_id}",
                    streak,
                    "No more than two consecutive exposition-only segments are allowed "
                    "at high humor intensity",
                )
            if exposition_only and segment.estimated_duration_ms > MAX_EXPOSITION_INTERVAL_MS:
                _error(
                    errors,
                    "LONG_EXPOSITION_WITHOUT_JOKE",
                    f"segments.{segment.segment_id}",
                    segment.estimated_duration_ms,
                    "No interval over 18s may lack a joke or visual gag at high humor intensity",
                )

    if previous_script is not None:
        previous_by_id = {segment.segment_id: segment for segment in previous_script.segments}
        for segment_id, previous in previous_by_id.items():
            if not previous.locked:
                continue
            current = segments_by_id.get(segment_id)
            if current is None or current.content_hash != previous.content_hash:
                _error(
                    errors,
                    "LOCKED_SEGMENT_CHANGED",
                    f"segments.{segment_id}",
                    segment_id,
                    "A locked segment must not change between revisions",
                )

    if previous_coverage is not None:
        current_coverage = {item.plot_beat_id: item.coverage for item in script.beat_coverage}
        for beat_id, coverage in previous_coverage.items():
            if (
                coverage == "covered"
                and current_coverage.get(beat_id) != "covered"
                and beat_id in mandatory_beat_ids
            ):
                _error(
                    errors,
                    "COVERAGE_REGRESSED",
                    "beat_coverage",
                    beat_id,
                    "A revision must not reduce mandatory beat coverage",
                )

    return ScriptValidationReport(valid=not errors, errors=errors)


def build_beat_coverage(script: RecapScript, plan: CompressedPlotPlan) -> list[BeatCoverage]:
    coverage: list[BeatCoverage] = []
    for beat in plan.selected_beats:
        segment_ids = [
            segment.segment_id
            for segment in script.segments
            if beat.plot_beat_id in segment.plot_beat_ids
        ]
        coverage.append(
            BeatCoverage(
                plot_beat_id=beat.plot_beat_id,
                segment_ids=segment_ids,
                coverage="covered" if segment_ids else "missing",
                mandatory=beat.mandatory,
            )
        )
    return coverage


def resolve_segment(segments: list[ScriptSegment], segment_id: UUID) -> ScriptSegment | None:
    return next((segment for segment in segments if segment.segment_id == segment_id), None)
