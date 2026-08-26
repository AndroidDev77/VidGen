from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID, uuid5

from services.analysis.transcript_scene_joiner import join_transcript_to_scene
from vidgen.contracts.evidence import (
    EvidenceDiagnostic,
    EvidencePackage,
    EvidenceProvenance,
    SceneEvidence,
    SourceTimeRange,
)
from vidgen.contracts.media import ExtractedFrame, SceneBoundary
from vidgen.contracts.transcription import TranscriptSegment

EVIDENCE_NAMESPACE = UUID("2c21c4c3-02da-4fda-b5ea-ac3ea3ccb37f")
BUILDER_VERSION = "t09.1"


def build_evidence_package(
    *,
    project_id: UUID,
    source_video_id: UUID,
    source_video_asset_id: UUID,
    source_audio_asset_id: UUID | None,
    transcript_id: UUID,
    transcript_asset_id: UUID,
    transcript_origin: Literal["subtitle", "audio_transcription"],
    subtitle_asset_id: UUID | None,
    scenes: Sequence[SceneBoundary],
    frames: Sequence[ExtractedFrame],
    segments: Sequence[TranscriptSegment],
    version: int = 1,
    contact_sheet_asset_id: UUID | None = None,
) -> EvidencePackage:
    canonical_inputs = json.dumps(
        {
            "builder": BUILDER_VERSION,
            "source": str(source_video_asset_id),
            "transcript": str(transcript_asset_id),
            "scenes": [scene.model_dump(mode="json") for scene in scenes],
            "frames": [frame.model_dump(mode="json") for frame in frames],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    input_hash = hashlib.sha256(canonical_inputs.encode()).hexdigest()
    diagnostics: list[EvidenceDiagnostic] = []
    evidence_scenes: list[SceneEvidence] = []
    for scene in sorted(scenes, key=lambda item: item.sequence):
        scene_frames = sorted(
            (frame for frame in frames if frame.scene_sequence == scene.sequence),
            key=lambda item: item.timestamp_seconds,
        )
        if not scene_frames:
            diagnostics.append(
                EvidenceDiagnostic(
                    code="MISSING_REPRESENTATIVE_FRAME",
                    severity="error",
                    message="scene has no representative frame asset",
                    scene_sequence=scene.sequence,
                )
            )
            continue
        evidence_scenes.append(
            SceneEvidence(
                scene_sequence=scene.sequence,
                source_range=SourceTimeRange(
                    start_seconds=scene.start_seconds, end_seconds=scene.end_seconds
                ),
                source_video_asset_id=source_video_asset_id,
                source_audio_asset_id=source_audio_asset_id,
                representative_frame_asset_ids=[frame.asset_id for frame in scene_frames],
                representative_frame_timestamps=[frame.timestamp_seconds for frame in scene_frames],
                transcript_items=join_transcript_to_scene(
                    scene, segments, transcript_asset_id=transcript_asset_id
                ),
            )
        )
    package_id = uuid5(EVIDENCE_NAMESPACE, f"{project_id}:{version}:{input_hash}")
    return EvidencePackage(
        package_id=package_id,
        project_id=project_id,
        version=version,
        source_video_id=source_video_id,
        source_video_asset_id=source_video_asset_id,
        contact_sheet_asset_id=contact_sheet_asset_id,
        scenes=evidence_scenes,
        provenance=EvidenceProvenance(
            transcript_origin=transcript_origin,
            transcript_id=transcript_id,
            transcript_asset_id=transcript_asset_id,
            subtitle_asset_id=subtitle_asset_id,
            input_hash=input_hash,
            builder_version=BUILDER_VERSION,
        ),
        diagnostics=diagnostics,
    )
