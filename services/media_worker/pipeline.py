from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.media_worker.audio import extract_transcription_audio
from services.media_worker.frames import extract_frame
from services.media_worker.probe import probe_media
from services.media_worker.scene_detect import detect_scenes
from vidgen.contracts.media import (
    AudioExtractionResult,
    ExtractedFrame,
    MediaProcessingResult,
    SceneBoundary,
)
from vidgen.db.models import Asset, AudioAsset, Project, Scene, SourceVideo
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore


class MediaPipeline:
    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self.session = session
        self.blob_store = blob_store
        self.assets = AssetService(session, blob_store)

    def process(
        self,
        *,
        project_id: UUID,
        source_video_id: UUID,
        idempotency_key: str,
        scene_threshold: float = 0.30,
    ) -> MediaProcessingResult:
        project = self.session.get(Project, project_id)
        source_video = self.session.get(SourceVideo, source_video_id)
        if project is None or source_video is None or source_video.project_id != project_id:
            raise ValueError("project or source video not found")
        source_asset = self.session.get(Asset, source_video.asset_id)
        if source_asset is None:
            raise ValueError("source asset not found")

        try:
            with TemporaryDirectory(prefix="vidgen-media-") as directory:
                workspace = Path(directory)
                source_path = workspace / "source.mp4"
                self.blob_store.copy_to(source_asset.storage_key, source_path)

                self._status(project, "probing")
                probe = probe_media(source_path)
                source_video.duration_seconds = probe.duration_seconds
                source_video.width = probe.video.width
                source_video.height = probe.video.height
                source_video.frame_rate = probe.video.frame_rate
                source_video.probe = probe.model_dump(mode="json")
                self.session.commit()

                self._status(project, "extracting_audio")
                audio_path = extract_transcription_audio(
                    source_path, workspace / "transcription.wav"
                )
                stored_audio = self.assets.store_file(
                    path=audio_path,
                    kind="audio",
                    media_type="audio/wav",
                    project_id=project.id,
                    parent_asset_ids=(source_asset.id,),
                    provider="ffmpeg",
                    idempotency_key=f"{idempotency_key}:audio",
                    generation_parameters={
                        "channels": 1,
                        "sample_rate": 16000,
                        "codec": "pcm_s16le",
                    },
                )
                audio_row = self.session.scalar(
                    select(AudioAsset).where(AudioAsset.asset_id == stored_audio.id)
                )
                if audio_row is None:
                    self.session.add(
                        AudioAsset(
                            project_id=project.id,
                            asset_id=stored_audio.id,
                            kind="transcription_audio",
                            duration_seconds=probe.duration_seconds,
                            provider="ffmpeg",
                        )
                    )
                self.session.commit()
                audio = AudioExtractionResult(
                    asset_id=stored_audio.id,
                    sha256=stored_audio.sha256,
                    duration_seconds=probe.duration_seconds,
                    sample_rate=16000,
                    channels=1,
                    codec="pcm_s16le",
                )

                self._status(project, "detecting_scenes")
                detection = detect_scenes(
                    source_path,
                    duration_seconds=probe.duration_seconds,
                    threshold=scene_threshold,
                )
                scenes = self._persist_scenes(project.id, detection.scenes)

                self._status(project, "extracting_frames")
                frames: list[ExtractedFrame] = []
                for boundary, scene in zip(detection.scenes, scenes, strict=True):
                    timestamp = (boundary.start_seconds + boundary.end_seconds) / 2
                    frame_path = extract_frame(
                        source_path,
                        timestamp,
                        workspace / f"scene-{boundary.sequence:05d}.png",
                    )
                    stored_frame = self.assets.store_file(
                        path=frame_path,
                        kind="frame",
                        media_type="image/png",
                        project_id=project.id,
                        parent_asset_ids=(source_asset.id,),
                        provider="ffmpeg",
                        idempotency_key=f"{idempotency_key}:frame:{boundary.sequence}",
                        generation_parameters={
                            "timestamp_seconds": timestamp,
                            "scene_sequence": boundary.sequence,
                        },
                    )
                    scene.analysis = {
                        **scene.analysis,
                        "representative_frame_asset_id": str(stored_frame.id),
                        "representative_timestamp_seconds": timestamp,
                    }
                    frames.append(
                        ExtractedFrame(
                            asset_id=stored_frame.id,
                            scene_sequence=boundary.sequence,
                            timestamp_seconds=timestamp,
                            sha256=stored_frame.sha256,
                            width=probe.video.width,
                            height=probe.video.height,
                        )
                    )
                self.session.commit()
                self._status(project, "media_ready")
                return MediaProcessingResult(
                    project_id=project.id,
                    source_video_id=source_video.id,
                    source_asset_id=source_asset.id,
                    probe=probe,
                    audio=audio,
                    scene_detection=detection,
                    frames=frames,
                )
        except Exception:
            self.session.rollback()
            project = self.session.get(Project, project_id)
            if project is not None:
                self._status(project, "media_failed")
            raise

    def _persist_scenes(self, project_id: UUID, boundaries: list[SceneBoundary]) -> list[Scene]:
        existing = {
            scene.sequence: scene
            for scene in self.session.scalars(select(Scene).where(Scene.project_id == project_id))
        }
        scenes: list[Scene] = []
        for boundary in boundaries:
            sequence = boundary.sequence
            scene = existing.get(sequence)
            if scene is None:
                scene = Scene(
                    project_id=project_id,
                    sequence=sequence,
                    source_start_seconds=boundary.start_seconds,
                    source_end_seconds=boundary.end_seconds,
                    summary=f"Detected scene {sequence + 1}",
                    analysis={},
                )
                self.session.add(scene)
            else:
                scene.source_start_seconds = boundary.start_seconds
                scene.source_end_seconds = boundary.end_seconds
            scenes.append(scene)
        self.session.commit()
        return scenes

    def _status(self, project: Project, value: str) -> None:
        project.status = value
        self.session.commit()
