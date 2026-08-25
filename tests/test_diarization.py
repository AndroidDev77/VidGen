from __future__ import annotations

from uuid import uuid4

from services.transcription.diarization import reconcile_speakers
from vidgen.contracts.transcription import AudioChunk, DiarizationResult, SpeakerTurn


def _chunk(sequence: int, start: float, end: float) -> AudioChunk:
    return AudioChunk(
        asset_id=uuid4(),
        parent_audio_asset_id=uuid4(),
        sequence=sequence,
        start_seconds=start,
        end_seconds=end,
        overlap_before_seconds=1 if sequence else 0,
        overlap_after_seconds=1,
        byte_size=10,
        sha256="b" * 64,
        codec="flac",
        sample_rate=16_000,
        idempotency_key=f"chunk-{sequence}",
    )


def _turn(label: str, start: float, end: float, chunk_id: object) -> SpeakerTurn:
    return SpeakerTurn(
        sequence=0,
        speaker_label=label,
        start_seconds=start,
        end_seconds=end,
        confidence=0.9,
        source_chunk_ids=[chunk_id],
        provider="fake",
        model="fake",
    )


def test_speaker_labels_reconcile_across_overlap() -> None:
    first = _chunk(0, 0, 4)
    second = _chunk(1, 3, 7)
    results = [
        (
            first,
            DiarizationResult(
                provider="fake",
                model="fake",
                provider_request_ids=["a"],
                turns=[_turn("speaker_001", 0, 4, first.asset_id)],
            ),
        ),
        (
            second,
            DiarizationResult(
                provider="fake",
                model="fake",
                provider_request_ids=["b"],
                turns=[_turn("speaker_001", 3, 7, second.asset_id)],
            ),
        ),
    ]
    turns, warnings = reconcile_speakers(results, duration_seconds=7)
    assert {turn.speaker_label for turn in turns} == {"speaker_001"}
    assert not warnings


def test_ambiguous_speaker_mapping_creates_anonymous_label() -> None:
    first = _chunk(0, 0, 4)
    second = _chunk(1, 3, 7)
    results = [
        (
            first,
            DiarizationResult(
                provider="fake",
                model="fake",
                provider_request_ids=["a"],
                turns=[
                    _turn("speaker_001", 0, 3.5, first.asset_id),
                    _turn("speaker_002", 3.5, 4, first.asset_id).model_copy(update={"sequence": 1}),
                ],
            ),
        ),
        (
            second,
            DiarizationResult(
                provider="fake",
                model="fake",
                provider_request_ids=["b"],
                turns=[_turn("speaker_001", 3, 4, second.asset_id)],
            ),
        ),
    ]
    turns, warnings = reconcile_speakers(results, duration_seconds=7)
    assert any(turn.speaker_label == "speaker_003" for turn in turns)
    assert any(warning.code == "ambiguous_speaker_mapping" for warning in warnings)
