from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from vidgen.contracts.transcription import ChunkTranscriptionResult, TranscriptWord

TOKEN = re.compile(r"[^\w']+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class MergeDiagnostic:
    chunk_sequence: int
    removed_words: int
    confidence: float
    method: str


def normalize_token(value: str) -> str:
    return TOKEN.sub("", value.casefold())


def merge_chunk_words(
    results: list[ChunkTranscriptionResult], *, maximum_alignment_words: int = 64
) -> tuple[list[TranscriptWord], list[MergeDiagnostic]]:
    ordered = sorted(results, key=lambda item: item.chunk.sequence)
    merged: list[TranscriptWord] = []
    diagnostics: list[MergeDiagnostic] = []
    previous: ChunkTranscriptionResult | None = None
    for result in ordered:
        _validate_words(result.words, result.chunk.start_seconds, result.chunk.end_seconds)
        words = result.words
        if previous is None or not merged or not words:
            merged.extend(words)
            diagnostics.append(MergeDiagnostic(result.chunk.sequence, 0, 1.0, "none"))
            previous = result
            continue

        overlap_start = result.chunk.start_seconds
        overlap_end = min(previous.chunk.end_seconds, result.chunk.end_seconds)
        previous_overlap = [word for word in merged if word.end_seconds > overlap_start]
        current_overlap = [word for word in words if word.start_seconds < overlap_end]
        remove, confidence, method = _alignment_count(
            previous_overlap[-maximum_alignment_words:],
            current_overlap[:maximum_alignment_words],
        )
        merged.extend(words[remove:])
        diagnostics.append(MergeDiagnostic(result.chunk.sequence, remove, confidence, method))
        previous = result
    _validate_words(merged, 0.0, float("inf"))
    return merged, diagnostics


def _alignment_count(
    previous: list[TranscriptWord], current: list[TranscriptWord]
) -> tuple[int, float, str]:
    if not previous or not current:
        return 0, 0.0, "none"
    maximum = min(len(previous), len(current))
    for length in range(maximum, 0, -1):
        left = [normalize_token(word.text) for word in previous[-length:]]
        right = [normalize_token(word.text) for word in current[:length]]
        if left == right and all(left):
            return length, 1.0, "exact"
    best_length = 0
    best_score = 0.0
    for length in range(maximum, 1, -1):
        left_text = " ".join(normalize_token(word.text) for word in previous[-length:])
        right_text = " ".join(normalize_token(word.text) for word in current[:length])
        score = SequenceMatcher(a=left_text, b=right_text, autojunk=False).ratio()
        timestamp_matches = sum(
            1
            for old, new in zip(previous[-length:], current[:length], strict=True)
            if abs(old.start_seconds - new.start_seconds) <= 0.75
        )
        combined = 0.75 * score + 0.25 * (timestamp_matches / length)
        if combined > best_score:
            best_length, best_score = length, combined
    if best_score >= 0.78:
        return best_length, best_score, "fuzzy"
    return 0, best_score, "none"


def _validate_words(words: list[TranscriptWord], lower: float, upper: float) -> None:
    previous_start = lower
    for word in words:
        if word.start_seconds < lower - 0.01 or word.end_seconds > upper + 0.01:
            raise ValueError("word timestamp falls outside its source chunk")
        if word.start_seconds + 0.01 < previous_start:
            raise ValueError("word timestamps reverse")
        previous_start = word.start_seconds
