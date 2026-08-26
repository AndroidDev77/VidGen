"""Deterministic plot compression: select the smallest causally complete plot.

This module implements the compression *algorithm* used by the deterministic fake
provider and is the reference the deterministic validator checks provider output
against. It never invents beats, dialogue, or facts: every selected beat, summary,
character, scene, and source reference is copied verbatim from the authoritative
T10 ``EpisodeAnalysis``.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from vidgen.contracts.episode_analysis import (
    EpisodeAnalysis,
    PlotBeat,
    SourceReference,
    StructuredNote,
)
from vidgen.contracts.script import (
    BeatWordAllocation,
    CompressedPlotBeat,
    CompressedPlotPlan,
    ConnectiveExplanation,
    OmittedPlotBeat,
    PacingAllocation,
    PlotCompressionRequest,
    StructuralRole,
    WordBudget,
)

MIN_BEATS = 12
MAX_BEATS = 20
CONFUSION_PAYOFF_THRESHOLD = 0.6


def structural_roles(beats: list[PlotBeat]) -> dict[UUID, StructuralRole]:
    """Deterministically classify structural beats from sequence and payoff alone.

    ``EpisodeAnalysis`` does not tag beats with a narrative phase, so the compressor
    and the validator both call this function so they always agree on which beats
    are structurally required.
    """
    ordered = sorted(beats, key=lambda beat: beat.sequence)
    if not ordered:
        return {}
    roles: dict[UUID, StructuralRole] = {}
    roles[ordered[0].plot_beat_id] = "setup"
    roles[ordered[-1].plot_beat_id] = "resolution"
    mandatory_ordered = [beat for beat in ordered if beat.mandatory] or ordered
    for beat in mandatory_ordered:
        if beat.plot_beat_id not in roles:
            roles[beat.plot_beat_id] = "inciting_incident"
            break
    candidates = [beat for beat in ordered if beat.plot_beat_id not in roles] or ordered
    climax = max(candidates, key=lambda beat: (beat.payoff_score, beat.sequence))
    roles.setdefault(climax.plot_beat_id, "climax")
    return roles


def _effect_to_causes(analysis: EpisodeAnalysis) -> dict[UUID, set[UUID]]:
    graph: dict[UUID, set[UUID]] = defaultdict(set)
    for dependency in analysis.beat_dependencies:
        graph[dependency.effect_beat_id].add(dependency.cause_beat_id)
    return graph


def causal_ancestors(beat_id: UUID, effect_to_causes: dict[UUID, set[UUID]]) -> set[UUID]:
    seen: set[UUID] = set()
    stack = [beat_id]
    while stack:
        current = stack.pop()
        for cause in effect_to_causes.get(current, ()):
            if cause not in seen:
                seen.add(cause)
                stack.append(cause)
    return seen


def compress_plot(
    *, analysis: EpisodeAnalysis, request: PlotCompressionRequest, plan_id: UUID
) -> CompressedPlotPlan:
    beats_by_id = {beat.plot_beat_id: beat for beat in analysis.plot_beats}
    if not beats_by_id:
        raise ValueError("episode analysis has no plot beats to compress")
    for beat_id in request.required_beat_ids:
        if beat_id not in beats_by_id:
            raise ValueError(f"required beat {beat_id} does not resolve in the episode analysis")

    roles = structural_roles(analysis.plot_beats)
    effect_to_causes = _effect_to_causes(analysis)
    excluded_topics = {topic.casefold() for topic in request.excluded_topics}

    def topic_excluded(beat: PlotBeat) -> bool:
        text = beat.summary.casefold()
        return any(topic and topic in text for topic in excluded_topics)

    required_ids: set[UUID] = {
        beat.plot_beat_id
        for beat in analysis.plot_beats
        if beat.mandatory or beat.plot_beat_id in roles
    }
    required_ids.update(request.required_beat_ids)
    excludable = {
        beat.plot_beat_id
        for beat in analysis.plot_beats
        if topic_excluded(beat) and beat.plot_beat_id not in required_ids
    }

    def close(ids: set[UUID]) -> set[UUID]:
        closed = set(ids)
        frontier = set(ids)
        while frontier:
            growth: set[UUID] = set()
            for beat_id in frontier:
                for cause in effect_to_causes.get(beat_id, ()):
                    if cause not in closed and cause not in excludable:
                        growth.add(cause)
            closed |= growth
            frontier = growth
        return closed

    selected_ids = close(required_ids)
    words_per_beat_estimate = max(1, request.target_words // max(len(selected_ids), 1))
    remaining = sorted(
        (
            beat
            for beat in analysis.plot_beats
            if beat.plot_beat_id not in selected_ids and beat.plot_beat_id not in excludable
        ),
        key=lambda beat: (-(beat.importance + beat.payoff_score), beat.sequence),
    )
    for beat in remaining:
        if len(selected_ids) >= MAX_BEATS:
            break
        candidate = close(selected_ids | {beat.plot_beat_id})
        if len(candidate) > MAX_BEATS:
            continue
        if len(selected_ids) >= MIN_BEATS and len(candidate) * words_per_beat_estimate > (
            request.target_words
        ):
            continue
        selected_ids = candidate

    selected_src = sorted((beats_by_id[i] for i in selected_ids), key=lambda beat: beat.sequence)
    omitted_src = [beat for beat in analysis.plot_beats if beat.plot_beat_id not in selected_ids]

    weights = [beat.importance + beat.payoff_score + 0.1 for beat in selected_src]
    total_weight = sum(weights) or 1.0
    raw_words = [max(1, round(weight / total_weight * request.target_words)) for weight in weights]
    drift = request.target_words - sum(raw_words)
    if raw_words:
        raw_words[-1] = max(1, raw_words[-1] + drift)
    wpm = request.target_words_per_minute
    durations = [max(1, round(words / wpm * 60_000)) for words in raw_words]

    selected_beats = [
        CompressedPlotBeat(
            plot_beat_id=beat.plot_beat_id,
            sequence=beat.sequence,
            summary=beat.summary,
            structural_role=roles.get(beat.plot_beat_id, "supporting"),
            mandatory=beat.mandatory,
            payoff_score=beat.payoff_score,
            character_ids=list(beat.character_ids),
            scene_ids=list(beat.scene_ids),
            source_references=list(beat.source_references) or list(analysis.source_references[:1]),
        )
        for beat in selected_src
    ]

    omitted_beats = []
    for beat in omitted_src:
        excluded = beat.plot_beat_id in excludable
        confuses = (not excluded) and beat.payoff_score >= CONFUSION_PAYOFF_THRESHOLD
        omitted_beats.append(
            OmittedPlotBeat(
                plot_beat_id=beat.plot_beat_id,
                reason=(
                    "Matches an excluded topic and is not required for causal completeness."
                    if excluded
                    else "Below the compression payoff/importance threshold for this target; "
                    "omission does not break the causal chain of selected beats."
                ),
                may_cause_confusion=confuses,
                confusion_explanation=(
                    f"'{beat.summary}' has a notable payoff that will not appear in the recap; "
                    "retained causal context for selected beats should keep later events clear."
                    if confuses
                    else None
                ),
            )
        )

    connective_explanations: list[ConnectiveExplanation] = []
    for dependency in analysis.beat_dependencies:
        if (
            dependency.cause_beat_id not in selected_ids
            or dependency.effect_beat_id not in selected_ids
        ):
            continue
        cause = beats_by_id[dependency.cause_beat_id]
        effect = beats_by_id[dependency.effect_beat_id]
        gap_has_omission = any(
            beats_by_id[b].sequence > cause.sequence and beats_by_id[b].sequence < effect.sequence
            for b in (beat.plot_beat_id for beat in omitted_src)
        )
        if gap_has_omission:
            connective_explanations.append(
                ConnectiveExplanation(
                    cause_beat_id=cause.plot_beat_id,
                    effect_beat_id=effect.plot_beat_id,
                    explanation=f"{cause.summary} directly leads to: {effect.summary}",
                )
            )

    word_budget = WordBudget(
        total_target_words=request.target_words,
        allocations=[
            BeatWordAllocation(
                plot_beat_id=beat.plot_beat_id, words=words, estimated_duration_ms=duration
            )
            for beat, words, duration in zip(selected_src, raw_words, durations, strict=True)
        ],
    )
    pacing_plan = [
        PacingAllocation(plot_beat_id=beat.plot_beat_id, estimated_duration_ms=duration)
        for beat, duration in zip(selected_src, durations, strict=True)
    ]

    seen_refs: dict[str, SourceReference] = {}
    for compressed_beat in selected_beats:
        for reference in compressed_beat.source_references:
            seen_refs[reference.model_dump_json()] = reference
    source_refs = list(seen_refs.values())

    first, last = selected_src[0], selected_src[-1]
    logline = analysis.logline or f"{first.summary} ... culminating in: {last.summary}"

    warnings = []
    if omitted_beats:
        warnings.append(
            StructuredNote(
                code="BEATS_OMITTED",
                message=f"{len(omitted_beats)} beat(s) were omitted to fit the target.",
            )
        )

    return CompressedPlotPlan(
        plan_id=plan_id,
        project_id=request.project_id,
        episode_analysis_id=request.episode_analysis_id,
        logline=logline,
        selected_beats=selected_beats,
        omitted_beats=omitted_beats,
        connective_explanations=connective_explanations,
        pacing_plan=pacing_plan,
        word_budget=word_budget,
        source_refs=source_refs,
        warnings=warnings,
    )
