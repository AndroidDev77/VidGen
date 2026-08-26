"""Approved narration split points derived from T11 script and T12 word timings.

The retimer may only split a shot at a boundary that already exists in the
approved script or its measured narration. Nothing here invents a boundary and
nothing here alters the approved text.
"""

from __future__ import annotations

from typing import Any

from services.storyboard.canonicalize import canonical_hash, seconds_to_us
from vidgen.contracts.storyboard import BoundaryKind, NarrationBoundary

_SENTENCE_MARKS = frozenset({".", "!", "?", "\u2026"})
_CLAUSE_MARKS = frozenset({",", ";", ":", "\u2014", "\u2013", "-"})


def word_boundaries(
    word_timings: list[dict[str, Any]], measured_duration_us: int
) -> list[NarrationBoundary]:
    """One boundary per measured narration word, at that word's end offset."""
    boundaries: list[NarrationBoundary] = []
    previous = 0
    for timing in sorted(word_timings, key=lambda item: int(item["word_index"])):
        offset = min(max(seconds_to_us(timing["end_seconds"]), previous), measured_duration_us)
        boundaries.append(
            NarrationBoundary(
                word_index=int(timing["word_index"]),
                offset_us=offset,
                kind=_kind(str(timing.get("punctuation", ""))),
                label=str(timing.get("word", ""))[:255],
            )
        )
        previous = offset
    if not boundaries:
        raise ValueError("narration segment has no measured word timings")
    return boundaries


def _kind(punctuation: str) -> BoundaryKind:
    if any(mark in punctuation for mark in _SENTENCE_MARKS):
        return "sentence"
    if any(mark in punctuation for mark in _CLAUSE_MARKS):
        return "clause"
    return "word"


def approved_boundaries(
    boundaries: list[NarrationBoundary],
    *,
    text: str,
    joke_annotations: list[dict[str, Any]],
) -> list[NarrationBoundary]:
    """Sentence and clause boundaries, plus comedy beat boundaries from T11.

    A beat boundary is the last word of an annotated setup or punchline span, so
    the retimer can prefer cutting on a comedic beat rather than mid-joke.
    """
    approved = [item for item in boundaries if item.kind in ("sentence", "clause")]
    beats = _beat_word_indices(text, joke_annotations, len(boundaries))
    by_index = {item.word_index: item for item in boundaries}
    for word_index in sorted(beats):
        source = by_index.get(word_index)
        if source is None:
            continue
        if any(item.word_index == word_index for item in approved):
            continue
        approved.append(
            NarrationBoundary(
                word_index=word_index,
                offset_us=source.offset_us,
                kind="beat",
                label=source.label,
            )
        )
    return sorted(approved, key=lambda item: item.word_index)


def _beat_word_indices(
    text: str, joke_annotations: list[dict[str, Any]], word_count: int
) -> set[int]:
    offsets = _word_character_offsets(text)
    indices: set[int] = set()
    for annotation in joke_annotations:
        for key in ("setup_span", "punchline_span"):
            span = annotation.get(key)
            if not isinstance(span, dict):
                continue
            end = span.get("end")
            if not isinstance(end, int):
                continue
            index = _word_index_at(offsets, end)
            if index is not None and 0 <= index < word_count:
                indices.add(index)
    return indices


def _word_character_offsets(text: str) -> list[tuple[int, int]]:
    """(start, end) character offsets for each whitespace-delimited word."""
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for word in text.split():
        start = text.index(word, cursor)
        end = start + len(word)
        offsets.append((start, end))
        cursor = end
    return offsets


def _word_index_at(offsets: list[tuple[int, int]], character: int) -> int | None:
    last: int | None = None
    for index, (start, end) in enumerate(offsets):
        if start < character:
            last = index
        if end >= character:
            break
    return last


def word_timing_hash(word_timings: list[dict[str, Any]]) -> str:
    """Bind exact word timings into the storyboard input identity."""
    return canonical_hash(
        [
            [
                int(timing["word_index"]),
                str(timing.get("comparison_token", timing.get("word", ""))),
                seconds_to_us(timing["start_seconds"]),
                seconds_to_us(timing["end_seconds"]),
            ]
            for timing in sorted(word_timings, key=lambda item: int(item["word_index"]))
        ]
    )
