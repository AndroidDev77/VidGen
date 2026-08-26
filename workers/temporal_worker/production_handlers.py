from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio import activity

from apps.api.settings import APISettings, get_settings
from packages.providers import FakeSubtitleProvider
from packages.workflows.activities import StageHandler
from services.analysis.contact_sheet import contact_sheet_manifest
from services.analysis.evidence_builder import build_evidence_package
from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.openai_adapter import OpenAIAnalysisConfig, OpenAIEpisodeAnalysisProvider
from services.analysis.pipeline import EpisodeAnalysisPipeline
from services.media_worker.pipeline import MediaPipeline
from services.narration.fake_provider import FakeNarrationProvider
from services.narration.alignment import OpenAIWhisperAligner
from services.narration.openai_adapter import OpenAINarrationProvider
from services.narration.pipeline import NarrationPipeline
from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.openai_adapter import OpenAIScriptConfig, OpenAIScriptGenerationProvider
from services.script.pipeline import ScriptGenerationPipeline
from services.subtitles.acquisition import TranscriptAcquisitionService
from services.subtitles.opensubtitles import OpenSubtitlesAdapter
from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from services.transcription.fake import FakeTranscriptionProvider
from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.contracts.media import ExtractedFrame, SceneBoundary
from vidgen.contracts.transcription import TranscriptSegment, TranscriptWord
from vidgen.contracts.workflow import StageActivityInput, StageActivityResult
from vidgen.db.models import Asset, AudioAsset, Project, Scene, SourceVideo, asset_dependencies
from vidgen.db.session import build_engine
from vidgen.db.subtitle_models import SubtitleCandidateRecord
from vidgen.db.transcription_models import (
    SpeakerTurnRecord,
    Transcript,
    TranscriptSegmentRecord,
)
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def build_production_handlers(
    settings: APISettings | None = None,
) -> dict[str, StageHandler]:
    """Build configured T05-T09 adapters used by the production worker."""
    configured = settings or get_settings()
    return {
        "upload": _with_session(configured, _validate_upload),
        "media_processing": _with_session(configured, _process_media),
        "transcript_acquisition": _with_session(configured, _acquire_transcript),
        "evidence": _with_session(configured, _build_evidence),
        "episode_analysis": _with_session(configured, _analyze_episode),
        "script_generation": _with_session(configured, _generate_script),
        "narration": _with_session(configured, _generate_narration),
    }


BusinessHandler = Callable[
    [Session, FilesystemBlobStore, APISettings, StageActivityInput], StageActivityResult
]


def _with_session(settings: APISettings, handler: BusinessHandler) -> StageHandler:
    engine = build_engine(settings.database_url)
    blob_store = FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())

    def execute(request: StageActivityInput) -> StageActivityResult:
        with Session(engine, expire_on_commit=False) as session:
            return handler(session, blob_store, settings, request)

    return execute


def _source(session: Session, request: StageActivityInput) -> SourceVideo:
    source = session.get(SourceVideo, request.source_video_id)
    if source is None or source.project_id != request.project_id:
        raise ValueError("project source video does not exist")
    if session.get(Asset, source.asset_id) is None:
        raise ValueError("source video asset does not exist")
    return source


def _validate_upload(
    session: Session,
    _blob_store: FilesystemBlobStore,
    _settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    source = _source(session, request)
    return StageActivityResult(
        stage=request.stage, entity_id=source.id, asset_id=source.asset_id, reused=True
    )


def _process_media(
    session: Session,
    blob_store: FilesystemBlobStore,
    _settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    result = MediaPipeline(session, blob_store).process(
        project_id=request.project_id,
        source_video_id=request.source_video_id,
        idempotency_key=request.idempotency_key,
    )
    return StageActivityResult(
        stage=request.stage, entity_id=result.source_video_id, asset_id=result.audio.asset_id
    )


def _acquire_transcript(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    source = _source(session, request)
    audio = _latest_audio(session, request.project_id, source.asset_id)
    if audio is None:
        raise ValueError("media processing did not create transcription audio")
    subtitle_provider = (
        OpenSubtitlesAdapter(
            api_key=settings.opensubtitles_api_key,
            username=settings.opensubtitles_username,
            password=settings.opensubtitles_password,
        )
        if settings.opensubtitles_api_key
        else (FakeSubtitleProvider() if settings.temporal_allow_fake_providers else None)
    )
    transcription_provider = (
        OpenAITranscriptionAdapter(
            api_key=settings.openai_api_key,
            transcription_model=settings.transcription_model,
            diarization_model=settings.diarization_model,
        )
        if settings.openai_api_key
        else (FakeTranscriptionProvider() if settings.temporal_allow_fake_providers else None)
    )

    async def acquire() -> StageActivityResult:
        try:
            subtitles = SubtitlePipeline(
                session,
                blob_store,
                subtitle_provider,
                config=SubtitlePipelineConfig(
                    languages=settings.subtitle_languages,
                    synchronize_provider_subtitles=settings.subtitle_sync_enabled,
                    allow_provider_search=settings.opensubtitles_api_key is not None,
                ),
            )
            transcription = (
                TranscriptionPipeline(session, blob_store, transcription_provider)
                if transcription_provider is not None
                else None
            )
            result = await TranscriptAcquisitionService(subtitles, transcription).process(
                project_id=request.project_id,
                source_video_id=source.id,
                source_audio_asset_id=audio.asset_id,
                idempotency_key=request.idempotency_key,
            )
            return StageActivityResult(
                stage=request.stage,
                entity_id=result.transcript_id,
                asset_id=result.transcript_asset_id,
            )
        finally:
            if isinstance(subtitle_provider, OpenSubtitlesAdapter):
                await subtitle_provider.close()
            if isinstance(transcription_provider, OpenAITranscriptionAdapter):
                await transcription_provider.close()

    return asyncio.run(acquire())


def _latest_audio(session: Session, project_id: UUID, source_asset_id: UUID) -> AudioAsset | None:
    candidates = session.scalars(
        select(AudioAsset)
        .where(AudioAsset.project_id == project_id, AudioAsset.kind == "transcription_audio")
        .order_by(AudioAsset.created_at.desc(), AudioAsset.id.desc())
    )
    for candidate in candidates:
        parent_ids = set(
            session.scalars(
                select(asset_dependencies.c.parent_asset_id).where(
                    asset_dependencies.c.asset_id == candidate.asset_id
                )
            )
        )
        if source_asset_id in parent_ids:
            return candidate
    return None


def _build_evidence(
    session: Session,
    blob_store: FilesystemBlobStore,
    _settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    source = _source(session, request)
    transcript = session.scalar(
        select(Transcript).where(Transcript.project_id == request.project_id, Transcript.selected)
    )
    if transcript is None:
        raise ValueError("project has no selected canonical transcript")
    existing = session.scalar(
        select(EvidencePackageRecord).where(
            EvidencePackageRecord.project_id == request.project_id,
            EvidencePackageRecord.provenance["idempotency_key"].as_string()
            == request.idempotency_key,
        )
    )
    if existing is not None:
        package_asset_id = UUID(str(existing.provenance["package_asset_id"]))
        if session.get(Asset, package_asset_id) is None:
            raise ValueError("persisted evidence package asset is missing")
        return StageActivityResult(
            stage=request.stage,
            entity_id=existing.id,
            asset_id=package_asset_id,
            reused=True,
        )
    scene_rows = list(
        session.scalars(
            select(Scene).where(Scene.project_id == request.project_id).order_by(Scene.sequence)
        )
    )
    segment_rows = list(
        session.scalars(
            select(TranscriptSegmentRecord)
            .where(TranscriptSegmentRecord.transcript_id == transcript.id)
            .order_by(TranscriptSegmentRecord.sequence)
        )
    )
    turn_rows = list(
        session.scalars(
            select(SpeakerTurnRecord)
            .where(SpeakerTurnRecord.transcript_id == transcript.id)
            .order_by(SpeakerTurnRecord.sequence)
        )
    )
    scenes = [
        SceneBoundary(
            sequence=row.sequence,
            start_seconds=row.source_start_seconds,
            end_seconds=row.source_end_seconds,
            confidence=1,
        )
        for row in scene_rows
    ]
    frames = [_frame(session, row) for row in scene_rows]
    segments = _segments_with_speakers(segment_rows, turn_rows)
    audio = _latest_audio(session, request.project_id, source.asset_id)
    subtitle_asset_id = _selected_subtitle_asset(session, transcript)
    origin: Literal["subtitle", "audio_transcription"] = (
        "subtitle" if transcript.subtitle_run_id is not None else "audio_transcription"
    )
    assets = AssetService(session, blob_store)
    sheet = assets.store(
        content=contact_sheet_manifest(frames),
        kind="json",
        media_type="application/vnd.vidgen.contact-sheet+json",
        project_id=request.project_id,
        parent_asset_ids=tuple(frame.asset_id for frame in frames),
        provider="vidgen",
        idempotency_key=f"{request.idempotency_key}:contact-sheet",
        generation_parameters={"columns": 4, "schema_version": "1.0"},
    )
    version = (
        session.scalar(
            select(EvidencePackageRecord.version)
            .where(EvidencePackageRecord.project_id == request.project_id)
            .order_by(EvidencePackageRecord.version.desc())
        )
        or 0
    ) + 1
    package = build_evidence_package(
        project_id=request.project_id,
        source_video_id=source.id,
        source_video_asset_id=source.asset_id,
        source_audio_asset_id=audio.asset_id if audio else None,
        transcript_id=transcript.id,
        transcript_asset_id=transcript.transcript_asset_id,
        transcript_origin=origin,
        subtitle_asset_id=subtitle_asset_id,
        scenes=scenes,
        frames=frames,
        segments=segments,
        version=version,
        contact_sheet_asset_id=sheet.id,
    )
    package_asset = assets.store(
        content=package.model_dump_json().encode(),
        kind="json",
        media_type="application/vnd.vidgen.evidence-package+json",
        project_id=request.project_id,
        parent_asset_ids=(source.asset_id, transcript.transcript_asset_id, sheet.id),
        provider="vidgen",
        idempotency_key=f"{request.idempotency_key}:package",
        generation_parameters={"builder_version": package.provenance.builder_version},
    )
    session.query(EvidencePackageRecord).filter_by(
        project_id=request.project_id, selected=True
    ).update({"selected": False})
    record = EvidencePackageRecord(
        id=package.package_id,
        project_id=request.project_id,
        version=version,
        selected=True,
        input_hash=package.provenance.input_hash,
        schema_version=package.schema_version,
        source_video_id=source.id,
        source_video_asset_id=source.asset_id,
        source_audio_asset_id=audio.asset_id if audio else None,
        transcript_id=transcript.id,
        transcript_asset_id=transcript.transcript_asset_id,
        transcript_origin=origin,
        subtitle_asset_id=subtitle_asset_id,
        contact_sheet_asset_id=sheet.id,
        provenance={
            **package.provenance.model_dump(mode="json"),
            "idempotency_key": request.idempotency_key,
            "package_asset_id": str(package_asset.id),
        },
    )
    session.add(record)
    session.flush()
    session.add_all(
        SceneEvidenceRecord(
            evidence_package_id=record.id,
            scene_sequence=item.scene_sequence,
            source_start_seconds=item.source_range.start_seconds,
            source_end_seconds=item.source_range.end_seconds,
            frame_asset_ids=[str(value) for value in item.representative_frame_asset_ids],
            evidence=item.model_dump(mode="json"),
        )
        for item in package.scenes
    )
    session.commit()
    return StageActivityResult(stage=request.stage, entity_id=record.id, asset_id=package_asset.id)


def _analyze_episode(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    evidence = session.scalar(
        select(EvidencePackageRecord).where(
            EvidencePackageRecord.project_id == request.project_id, EvidencePackageRecord.selected
        )
    )
    if evidence is None:
        raise ValueError("project has no selected evidence package")
    provider = (
        OpenAIEpisodeAnalysisProvider(
            OpenAIAnalysisConfig(api_key=settings.openai_api_key, model=settings.analysis_model)
        )
        if settings.openai_api_key
        else FakeEpisodeAnalysisProvider()
    )
    if not settings.openai_api_key and not settings.temporal_allow_fake_providers:
        raise ValueError("episode analysis provider is not configured")
    result = asyncio.run(
        EpisodeAnalysisPipeline(session, blob_store, provider).process(
            project_id=request.project_id,
            evidence_package_id=evidence.id,
            idempotency_key=request.idempotency_key,
        )
    )
    return StageActivityResult(
        stage=request.stage,
        entity_id=result.episode_analysis_id,
        asset_id=result.analysis_asset_id,
        reused=False,
    )


def _generate_script(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    provider = (
        OpenAIScriptGenerationProvider(
            OpenAIScriptConfig(
                api_key=settings.openai_api_key,
                compressor_model=settings.script_compressor_model,
                writer_model=settings.script_writer_model,
                editor_model=settings.script_editor_model,
            )
        )
        if settings.openai_api_key
        else FakeScriptGenerationProvider()
    )
    if not settings.openai_api_key and not settings.temporal_allow_fake_providers:
        raise ValueError("script generation provider is not configured")
    result = asyncio.run(
        ScriptGenerationPipeline(session, blob_store, provider).process(
            project_id=request.project_id, idempotency_key=request.idempotency_key
        )
    )
    return StageActivityResult(
        stage=request.stage,
        entity_id=result.script_id,
        asset_id=None,
        reused=False,
    )


def _generate_narration(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    project = session.get(Project, request.project_id)
    if project is None:
        raise ValueError("project does not exist")
    try:
        voice_profile_id = UUID(str(project.settings["voice_profile_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("project settings require a valid voice_profile_id") from error
    if settings.openai_api_key:
        provider = OpenAINarrationProvider(settings.openai_api_key)
    elif settings.temporal_allow_fake_providers:
        provider = FakeNarrationProvider()
    else:
        raise ValueError("narration provider is not configured")
    result = asyncio.run(
        NarrationPipeline(
            session,
            blob_store,
            provider,
            aligner=OpenAIWhisperAligner(settings.openai_api_key)
            if settings.openai_api_key
            else None,
            cancellation_check=activity.is_cancelled,
        ).process(
            project_id=request.project_id,
            voice_profile_id=voice_profile_id,
            idempotency_key=request.idempotency_key,
        )
    )
    return StageActivityResult(
        stage=request.stage,
        entity_id=result.narration_run_id,
        asset_id=result.preview_manifest_asset_id,
        reused=False,
    )


def _frame(session: Session, scene: Scene) -> ExtractedFrame:
    try:
        asset_id = UUID(str(scene.analysis["representative_frame_asset_id"]))
        timestamp = float(scene.analysis["representative_timestamp_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"scene {scene.sequence} has no valid representative frame") from error
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ValueError(f"scene {scene.sequence} representative frame asset is missing")
    return ExtractedFrame(
        asset_id=asset.id,
        scene_sequence=scene.sequence,
        timestamp_seconds=timestamp,
        sha256=asset.sha256,
        width=int(asset.extra_metadata.get("width", 1)),
        height=int(asset.extra_metadata.get("height", 1)),
    )


def _selected_subtitle_asset(session: Session, transcript: Transcript) -> UUID | None:
    if transcript.subtitle_run_id is None:
        return None
    return session.scalar(
        select(SubtitleCandidateRecord.asset_id).where(
            SubtitleCandidateRecord.run_id == transcript.subtitle_run_id,
            SubtitleCandidateRecord.selected,
        )
    )


def _segments_with_speakers(
    segments: list[TranscriptSegmentRecord], turns: list[SpeakerTurnRecord]
) -> list[TranscriptSegment]:
    """Split audio segments at diarization turns while preserving overlaps."""
    result: list[TranscriptSegment] = []
    for segment in segments:
        overlapping = [
            turn
            for turn in turns
            if turn.start_seconds < segment.end_seconds and turn.end_seconds > segment.start_seconds
        ]
        if segment.speaker_label is not None or not overlapping:
            result.append(_contract_segment(segment))
            continue
        for turn in overlapping:
            result.append(
                TranscriptSegment(
                    sequence=segment.sequence,
                    start_seconds=max(segment.start_seconds, turn.start_seconds),
                    end_seconds=min(segment.end_seconds, turn.end_seconds),
                    text=segment.text,
                    speaker_label=turn.speaker_label,
                    confidence=turn.confidence,
                    source_chunk_ids=[UUID(value) for value in turn.source_chunk_ids],
                )
            )
    return result


def _contract_segment(segment: TranscriptSegmentRecord) -> TranscriptSegment:
    return TranscriptSegment(
        sequence=segment.sequence,
        start_seconds=segment.start_seconds,
        end_seconds=segment.end_seconds,
        text=segment.text,
        speaker_label=segment.speaker_label,
        confidence=segment.confidence,
        source_chunk_ids=[UUID(value) for value in segment.source_chunk_ids],
        words=[TranscriptWord.model_validate(word) for word in segment.words],
    )
