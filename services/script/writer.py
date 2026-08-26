"""Deterministic comedy writer: turn a CompressedPlotPlan into a RecapScript.

Like the compressor, this is the concrete algorithm the deterministic fake provider
runs, and the reference shape a real LLM adapter's output is checked against by
``services.script.validator``. It never introduces plot facts: every segment is
built from the beat summary already present in the compressed plan.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from services.script.canonicalize import compute_segment_content_hash
from services.script.validator import build_beat_coverage, canonical_word_count
from vidgen.contracts.script import (
    Callback,
    ComedyWritingRequest,
    CompressedPlotPlan,
    JokeAnnotation,
    JokeType,
    RecapScript,
    ScriptSegment,
    TextSpan,
)

WRITER_NAMESPACE = UUID("7a1b5e40-6b1e-4f7b-9a1a-3c2f2f3f9a11")

_JOKE_MECHANISMS: tuple[JokeType, ...] = (
    "commentary",
    "analogy",
    "exaggeration",
    "contrast",
    "character_observation",
    "wordplay",
)

_JOKE_CLAUSES: dict[JokeType, str] = {
    "commentary": "Real subtle, everyone.",
    "analogy": "Basically a soap opera with worse lighting.",
    "exaggeration": "A genuinely history-altering event, allegedly.",
    "contrast": "Meanwhile, nobody thought to just talk it out.",
    "character_observation": "Classic behavior, honestly.",
    "wordplay": "Talk about a plot twist and a half.",
}

_FILLER_BANK = (
    "Anyway,",
    "somehow,",
    "everyone",
    "just",
    "went",
    "along",
    "with",
    "it,",
    "naturally.",
)


def _fit_segment_text(summary: str, joke_clause: str, target_words: int) -> tuple[str, int, int]:
    target_words = max(1, target_words)
    summary_words = summary.split()
    joke_words = joke_clause.split()
    # Truncate the joke clause to the target first so it can never alone exceed
    # the budget, then give whatever remains (possibly nothing) to the summary;
    # a fixed reservation for either side can force the combined total past
    # target_words when the other side is already at its cap.
    if len(joke_words) > target_words:
        joke_words = joke_words[:target_words]
    budget_for_summary = target_words - len(joke_words)
    if len(summary_words) > budget_for_summary:
        summary_words = summary_words[:budget_for_summary]
    words = [*summary_words, *joke_words]
    filler_index = 0
    while len(words) < target_words:
        words.append(_FILLER_BANK[filler_index % len(_FILLER_BANK)])
        filler_index += 1
    full_text = " ".join(words)
    prefix = " ".join(summary_words)
    joke_start = len(prefix) + (1 if prefix else 0)
    joke_end = joke_start + len(" ".join(joke_words))
    return full_text, joke_start, joke_end


def write_script(
    *,
    plan: CompressedPlotPlan,
    request: ComedyWritingRequest,
    script_id: UUID,
    version: int = 1,
    parent_script_id: UUID | None = None,
) -> RecapScript:
    if not plan.selected_beats:
        raise ValueError("compressed plot plan has no selected beats to write from")
    locked_by_beat = {
        beat_id: segment for segment in request.locked_segments for beat_id in segment.plot_beat_ids
    }
    words_by_beat = {item.plot_beat_id: item.words for item in plan.word_budget.allocations}
    duration_by_beat = {item.plot_beat_id: item.estimated_duration_ms for item in plan.pacing_plan}
    voice_direction = "upbeat, wry" if request.humor_intensity >= 0.7 else "measured, dry"

    segments: list[ScriptSegment] = []
    reused_segment_ids: set[UUID] = set()
    for index, beat in enumerate(plan.selected_beats):
        reused = locked_by_beat.get(beat.plot_beat_id)
        if reused is not None and reused.locked and reused.segment_id not in reused_segment_ids:
            # A locked segment keeps its content and identity, but is re-sequenced
            # to its current position; a segment tagged with more than one beat ID
            # must only be appended once even though every one of its beats maps
            # to it in ``locked_by_beat``.
            reused_segment_ids.add(reused.segment_id)
            segments.append(reused.model_copy(update={"sequence": index}))
            continue
        mechanism = _JOKE_MECHANISMS[index % len(_JOKE_MECHANISMS)]
        target_words = words_by_beat.get(beat.plot_beat_id, 20)
        text, joke_start, joke_end = _fit_segment_text(
            beat.summary, _JOKE_CLAUSES[mechanism], target_words
        )
        segment_id = uuid5(WRITER_NAMESPACE, f"{plan.plan_id}:segment:{beat.plot_beat_id}")
        joke_id = uuid5(WRITER_NAMESPACE, f"{segment_id}:joke:0")
        joke_annotations = [
            JokeAnnotation(
                joke_id=joke_id,
                joke_type=mechanism,
                setup_span=TextSpan(start=0, end=max(1, joke_start - 1))
                if joke_start > 0
                else None,
                punchline_span=TextSpan(start=joke_start, end=joke_end),
                source_beat_ids=[beat.plot_beat_id],
                confidence=1.0,
                validation_status="valid",
            )
        ]
        content_hash = compute_segment_content_hash(
            text=text,
            segment_type="NARRATION",
            speaker_kind="narrator",
            speaker_character_id=None,
            anonymous_speaker_label=None,
            joke_annotations=[item.model_dump(mode="json") for item in joke_annotations],
            visual_gag=None,
            voice_direction=voice_direction,
        )
        segments.append(
            ScriptSegment(
                segment_id=segment_id,
                sequence=index,
                type="NARRATION",
                speaker_kind="narrator",
                text=text,
                plot_beat_ids=[beat.plot_beat_id],
                source_scene_ids=list(beat.scene_ids),
                joke_annotations=joke_annotations,
                visual_gag=(
                    f"Visual gag: quick cut reaction shot for '{beat.summary[:40]}'"
                    if index % 3 == 0
                    else None
                ),
                estimated_duration_ms=duration_by_beat.get(beat.plot_beat_id, 4_000),
                voice_direction=voice_direction,
                locked=False,
                content_hash=content_hash,
            )
        )

    callbacks: list[Callback] = []
    setup_span = (
        segments[0].joke_annotations[0].punchline_span
        if segments and segments[0].joke_annotations
        else None
    )
    if len(segments) >= 2 and setup_span is not None:
        setup_segment = segments[0]
        payoff_segment = segments[-1]
        callback_id = uuid5(WRITER_NAMESPACE, f"{plan.plan_id}:callback:0")
        callback_clause = (
            f" And speaking of that: {setup_segment.text[setup_span.start : setup_span.end]}"
        )
        new_text = payoff_segment.text + callback_clause
        callback_joke = JokeAnnotation(
            joke_id=uuid5(WRITER_NAMESPACE, f"{payoff_segment.segment_id}:joke:callback"),
            joke_type="callback",
            punchline_span=TextSpan(start=len(payoff_segment.text) + 1, end=len(new_text)),
            callback_id=callback_id,
            source_beat_ids=payoff_segment.plot_beat_ids,
            confidence=1.0,
            validation_status="valid",
        )
        updated_annotations = [*payoff_segment.joke_annotations, callback_joke]
        new_hash = compute_segment_content_hash(
            text=new_text,
            segment_type=payoff_segment.type,
            speaker_kind=payoff_segment.speaker_kind,
            speaker_character_id=payoff_segment.speaker_character_id,
            anonymous_speaker_label=payoff_segment.anonymous_speaker_label,
            joke_annotations=[item.model_dump(mode="json") for item in updated_annotations],
            visual_gag=payoff_segment.visual_gag,
            voice_direction=payoff_segment.voice_direction,
        )
        segments[-1] = payoff_segment.model_copy(
            update={
                "text": new_text,
                "joke_annotations": updated_annotations,
                "content_hash": new_hash,
            }
        )
        callbacks.append(
            Callback(
                callback_id=callback_id,
                setup_segment_id=setup_segment.segment_id,
                payoff_segment_id=payoff_segment.segment_id,
                description="The cold open's opening jab pays off in the closing beat.",
            )
        )

    actual_word_count = sum(canonical_word_count(segment.text) for segment in segments)
    cold_open_text = (
        "Previously, on a show that definitely had consequences: chaos."
        if request.recap_mode == "full_recap"
        else None
    )
    draft = RecapScript(
        script_id=script_id,
        version=version,
        parent_script_id=parent_script_id,
        project_id=request.project_id,
        episode_analysis_id=request.episode_analysis_id,
        compressed_plot_plan_id=request.compressed_plot_plan_id,
        target_duration_ms=sum(item.estimated_duration_ms for item in plan.pacing_plan),
        target_word_count=request.target_words,
        actual_word_count=actual_word_count,
        voice_profile_ref=request.channel_voice.narrator_persona,
        humor_intensity=request.humor_intensity,
        cold_open_text=cold_open_text,
        segments=segments,
        callbacks=callbacks,
        beat_coverage=[],
        source_refs=list(plan.source_refs),
    )
    coverage = build_beat_coverage(draft, plan)
    return draft.model_copy(update={"beat_coverage": coverage})
