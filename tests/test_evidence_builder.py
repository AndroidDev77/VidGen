from uuid import uuid4

from services.analysis.contact_sheet import contact_sheet_hash
from services.analysis.evidence_builder import build_evidence_package
from vidgen.contracts.media import ExtractedFrame, SceneBoundary
from vidgen.contracts.transcription import TranscriptSegment


def test_boundary_spanning_transcript_and_deterministic_package() -> None:
    project_id, video_id, video_asset, audio_asset, transcript_id, transcript_asset = (
        uuid4() for _ in range(6)
    )
    scenes = [
        SceneBoundary(sequence=0, start_seconds=0, end_seconds=5, confidence=1),
        SceneBoundary(sequence=1, start_seconds=5, end_seconds=10, confidence=1),
    ]
    frames = [
        ExtractedFrame(
            asset_id=uuid4(),
            scene_sequence=i,
            timestamp_seconds=i * 5 + 2,
            sha256="a" * 64,
            width=1,
            height=1,
        )
        for i in range(2)
    ]
    segment = TranscriptSegment(
        sequence=0,
        start_seconds=4,
        end_seconds=6,
        text="overlap",
        speaker_label="speaker_000",
        source_chunk_ids=[uuid4()],
    )
    kwargs = dict(
        project_id=project_id,
        source_video_id=video_id,
        source_video_asset_id=video_asset,
        source_audio_asset_id=audio_asset,
        transcript_id=transcript_id,
        transcript_asset_id=transcript_asset,
        transcript_origin="subtitle",
        subtitle_asset_id=uuid4(),
        scenes=scenes,
        frames=frames,
        segments=[segment],
    )
    first = build_evidence_package(**kwargs)  # type: ignore[arg-type]
    second = build_evidence_package(**kwargs)  # type: ignore[arg-type]
    assert first == second
    assert [item.source_range.end_seconds for item in first.scenes[0].transcript_items] == [5]
    assert [item.source_range.start_seconds for item in first.scenes[1].transcript_items] == [5]
    assert contact_sheet_hash(frames) == contact_sheet_hash(reversed(frames))


def test_scene_without_frame_produces_structured_diagnostic() -> None:
    ids = [uuid4() for _ in range(5)]
    package = build_evidence_package(
        project_id=ids[0],
        source_video_id=ids[1],
        source_video_asset_id=ids[2],
        source_audio_asset_id=None,
        transcript_id=ids[3],
        transcript_asset_id=ids[4],
        transcript_origin="audio_transcription",
        subtitle_asset_id=None,
        scenes=[SceneBoundary(sequence=0, start_seconds=0, end_seconds=1, confidence=1)],
        frames=[],
        segments=[],
    )
    assert package.scenes == []
    assert package.diagnostics[0].code == "MISSING_REPRESENTATIVE_FRAME"
