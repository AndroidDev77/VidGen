from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from vidgen.contracts.evidence import EvidenceTranscriptItem, SourceTimeRange
from vidgen.contracts.media import SceneBoundary
from vidgen.contracts.transcription import TranscriptSegment


def join_transcript_to_scene(
    scene: SceneBoundary,
    segments: Sequence[TranscriptSegment],
    *,
    transcript_asset_id: UUID,
) -> list[EvidenceTranscriptItem]:
    """Return every segment overlapping a scene, clipped to the scene interval."""
    joined: list[EvidenceTranscriptItem] = []
    for segment in segments:
        start = max(scene.start_seconds, segment.start_seconds)
        end = min(scene.end_seconds, segment.end_seconds)
        if start >= end:
            continue
        joined.append(
            EvidenceTranscriptItem(
                source_range=SourceTimeRange(start_seconds=start, end_seconds=end),
                source_asset_id=transcript_asset_id,
                text=segment.text,
                speaker_label=segment.speaker_label,
                confidence=segment.confidence,
                segment_sequence=segment.sequence,
            )
        )
    return joined
