from __future__ import annotations

from uuid import uuid4

import pytest

from services.transcription.overlap_merge import merge_chunk_words
from vidgen.contracts.transcription import (
    AudioChunk,
    ChunkTranscriptionResult,
    TranscriptWord,
)


def _result(
    sequence: int, start: float, end: float, values: list[tuple[str, float, float]]
) -> ChunkTranscriptionResult:
    chunk = AudioChunk(
        asset_id=uuid4(),
        parent_audio_asset_id=uuid4(),
        sequence=sequence,
        start_seconds=start,
        end_seconds=end,
        overlap_before_seconds=1 if sequence else 0,
        overlap_after_seconds=1,
        byte_size=100,
        sha256="a" * 64,
        codec="flac",
        sample_rate=16_000,
        idempotency_key=f"chunk-{sequence}",
    )
    words = [TranscriptWord(text=text, start_seconds=a, end_seconds=b) for text, a, b in values]
    return ChunkTranscriptionResult(
        chunk=chunk,
        provider="fake",
        model="fake",
        provider_request_id=f"request-{sequence}",
        attempt=1,
        text=" ".join(item[0] for item in values),
        segments=[],
        words=words,
    )


def test_exact_and_punctuation_overlap_is_removed() -> None:
    first = _result(0, 0, 3, [("Hello,", 0, 1), ("there", 1, 2), ("friend", 2, 3)])
    second = _result(1, 2, 5, [("FRIEND!", 2, 3), ("welcome", 3, 4), ("back", 4, 5)])
    words, diagnostics = merge_chunk_words([first, second])
    assert [word.text for word in words] == ["Hello,", "there", "friend", "welcome", "back"]
    assert diagnostics[1].removed_words == 1


def test_fuzzy_overlap_and_three_chunks_are_deterministic() -> None:
    first = _result(0, 0, 3, [("we", 0, 1), ("are", 1, 2), ("ready", 2, 3)])
    second = _result(1, 1, 5, [("we're", 1, 2), ("ready", 2, 3), ("to", 3, 4), ("go", 4, 5)])
    third = _result(2, 4, 7, [("go", 4, 5), ("right", 5, 6), ("now", 6, 7)])
    first_merge = merge_chunk_words([third, first, second])
    second_merge = merge_chunk_words([first, second, third])
    assert first_merge == second_merge
    assert [word.text for word in first_merge[0]][-3:] == ["go", "right", "now"]


def test_timestamp_reversal_is_rejected() -> None:
    with pytest.raises(ValueError, match="ordered"):
        _result(0, 0, 3, [("later", 2, 3), ("earlier", 1, 2)])
