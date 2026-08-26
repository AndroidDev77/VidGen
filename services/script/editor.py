"""Targeted, deterministic revision heuristic used by the fake comedy editor.

A real provider decides what to punch up; this module is the concrete stand-in that
lets the pipeline's revision loop be exercised without paid API calls. It only ever
touches unlocked segments and never introduces new plot facts.
"""

from __future__ import annotations

from vidgen.contracts.script import JokeAnnotation, RecapScript, ScriptEdit, TextSpan
from services.script.canonicalize import compute_segment_content_hash
from services.script.rubric import LONG_SEGMENT_WORDS
from services.script.validator import canonical_word_count
from services.script.writer import _FILLER_BANK

_FILLER_WORDS = frozenset(_FILLER_BANK)


def _relocate_span(old_text: str, new_text: str, span: TextSpan) -> TextSpan | None:
    snippet = old_text[span.start : span.end]
    position = new_text.find(snippet)
    if position == -1:
        return None
    return TextSpan(start=position, end=position + len(snippet))


def _relocate_joke(old_text: str, new_text: str, joke: JokeAnnotation) -> JokeAnnotation | None:
    updates: dict[str, object] = {}
    for field in ("setup_span", "punchline_span"):
        span = getattr(joke, field)
        if span is None:
            continue
        relocated = _relocate_span(old_text, new_text, span)
        if relocated is None:
            return None
        updates[field] = relocated
    return joke.model_copy(update=updates) if updates else joke


def propose_revision(script: RecapScript) -> tuple[list[ScriptEdit], RecapScript]:
    """Return (edits, revised_script). Empty edits means nothing left to try."""
    unlocked = [segment for segment in script.segments if not segment.locked]
    if not unlocked:
        return [], script
    target = max(unlocked, key=lambda segment: canonical_word_count(segment.text))
    if canonical_word_count(target.text) <= LONG_SEGMENT_WORDS:
        return [], script

    old_words = target.text.split()
    new_words = [word for word in old_words if word not in _FILLER_WORDS]
    if len(new_words) == len(old_words):
        return [], script
    new_text = " ".join(new_words)

    relocated_jokes: list[JokeAnnotation] = []
    for joke in target.joke_annotations:
        relocated = _relocate_joke(target.text, new_text, joke)
        if relocated is None:
            return [], script
        relocated_jokes.append(relocated)

    new_hash = compute_segment_content_hash(
        text=new_text,
        segment_type=target.type,
        speaker_kind=target.speaker_kind,
        speaker_character_id=target.speaker_character_id,
        anonymous_speaker_label=target.anonymous_speaker_label,
        joke_annotations=[joke.model_dump(mode="json") for joke in relocated_jokes],
        visual_gag=target.visual_gag,
        voice_direction=target.voice_direction,
    )
    revised_segment = target.model_copy(
        update={"text": new_text, "content_hash": new_hash, "joke_annotations": relocated_jokes}
    )
    new_segments = [
        revised_segment if segment.segment_id == target.segment_id else segment
        for segment in script.segments
    ]
    actual_word_count = sum(canonical_word_count(segment.text) for segment in new_segments)
    edit = ScriptEdit(
        segment_id=target.segment_id,
        old_text=target.text,
        new_text=new_text,
        reason="Trimmed unnecessary lead-in/filler to tighten spoken rhythm and pacing.",
        rubric_dimensions=["spoken_rhythm", "pacing"],
        plot_beat_ids=list(target.plot_beat_ids),
        changes_word_count=True,
        was_locked=False,
    )
    revised_script = script.model_copy(
        update={"segments": new_segments, "actual_word_count": actual_word_count}
    )
    return [edit], revised_script
