from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from vidgen.contracts.transcription import (
    AudioChunk,
    DiarizationResult,
    SpeakerTurn,
    TranscriptionWarning,
)


def reconcile_speakers(
    chunk_results: list[tuple[AudioChunk, DiarizationResult]],
    *,
    duration_seconds: float,
    merge_gap_seconds: float = 0.25,
) -> tuple[list[SpeakerTurn], list[TranscriptionWarning]]:
    global_turns: list[SpeakerTurn] = []
    warnings: list[TranscriptionWarning] = []
    next_label = 1
    for chunk, result in sorted(chunk_results, key=lambda item: item[0].sequence):
        local_groups: dict[str, list[SpeakerTurn]] = defaultdict(list)
        for turn in result.turns:
            if turn.start_seconds < 0 or turn.end_seconds > duration_seconds + 0.01:
                raise ValueError("speaker turn falls outside source duration")
            local_groups[turn.speaker_label].append(turn)
        mapping: dict[str, str] = {}
        alternatives: dict[str, list[str]] = {}
        for local_label, local_turns in sorted(local_groups.items()):
            scores: dict[str, float] = defaultdict(float)
            for local in local_turns:
                for existing in global_turns:
                    overlap = _overlap(
                        local.start_seconds,
                        local.end_seconds,
                        existing.start_seconds,
                        existing.end_seconds,
                    )
                    if overlap > 0:
                        scores[existing.speaker_label] += overlap
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1] * 1.25):
                mapping[local_label] = ranked[0][0]
                alternatives[local_label] = [label for label, _ in ranked[1:]]
            else:
                mapping[local_label] = f"speaker_{next_label:03d}"
                alternatives[local_label] = [label for label, _ in ranked]
                next_label += 1
                if len(ranked) > 1:
                    warnings.append(
                        TranscriptionWarning(
                            code="ambiguous_speaker_mapping",
                            message=f"created a new anonymous label for {local_label}",
                            chunk_sequence=chunk.sequence,
                        )
                    )
        for turn in result.turns:
            global_turns.append(
                turn.model_copy(
                    update={
                        "speaker_label": mapping[turn.speaker_label],
                        "source_chunk_ids": list(
                            dict.fromkeys([*turn.source_chunk_ids, chunk.asset_id])
                        ),
                        "alternate_labels": alternatives[turn.speaker_label],
                    }
                )
            )
    global_turns.sort(key=lambda item: (item.start_seconds, item.end_seconds, item.speaker_label))
    return _merge_adjacent(global_turns, merge_gap_seconds), warnings


def _merge_adjacent(turns: list[SpeakerTurn], gap: float) -> list[SpeakerTurn]:
    merged: list[SpeakerTurn] = []
    for turn in turns:
        if (
            merged
            and merged[-1].speaker_label == turn.speaker_label
            and 0 <= turn.start_seconds - merged[-1].end_seconds <= gap
        ):
            previous = merged[-1]
            source_ids: list[UUID] = list(
                dict.fromkeys([*previous.source_chunk_ids, *turn.source_chunk_ids])
            )
            merged[-1] = previous.model_copy(
                update={
                    "end_seconds": turn.end_seconds,
                    "source_chunk_ids": source_ids,
                    "confidence": _minimum_confidence(previous.confidence, turn.confidence),
                }
            )
        else:
            merged.append(turn.model_copy(update={"sequence": len(merged)}))
    return [turn.model_copy(update={"sequence": index}) for index, turn in enumerate(merged)]


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _minimum_confidence(first: float | None, second: float | None) -> float | None:
    values = [value for value in (first, second) if value is not None]
    return min(values) if values else None
