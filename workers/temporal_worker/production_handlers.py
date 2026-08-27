from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio import activity

from apps.api.settings import APISettings, get_settings
from packages.providers import FakeSubtitleProvider
from packages.providers.image_generation import DeterministicFakeImageProvider
from packages.workflows.activities import StageHandler
from packages.workflows.shot_activities import ShotActivityHandler
from packages.workflows.shot_policy import identity_hash, shot_activity_idempotency_key
from services.analysis.contact_sheet import contact_sheet_manifest
from services.analysis.evidence_builder import build_evidence_package
from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.openai_adapter import OpenAIAnalysisConfig, OpenAIEpisodeAnalysisProvider
from services.analysis.pipeline import EpisodeAnalysisPipeline
from services.animation.fake_provider import FakeVideoProvider
from services.animation.pipeline import PIPELINE_VERSION as T15_PIPELINE_VERSION
from services.animation.pipeline import AnimationPipeline
from services.animation.providers import VideoGenerationProvider
from services.animation.runway import RunwayVideoProvider
from services.image_generation.openai_image import OpenAIImageProvider
from services.image_generation.pipeline import (
    PIPELINE_VERSION as T14_PIPELINE_VERSION,
)
from services.image_generation.pipeline import (
    ImageGenerationPipeline,
)
from services.image_generation.providers import ImageGenerationProvider
from services.media_worker.pipeline import MediaPipeline
from services.narration.alignment import OpenAIWhisperAligner
from services.narration.fake_provider import FakeNarrationProvider
from services.narration.openai_adapter import OpenAINarrationProvider
from services.narration.pipeline import NarrationPipeline
from services.narration.providers import NarrationProvider
from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.openai_adapter import OpenAIScriptConfig, OpenAIScriptGenerationProvider
from services.script.pipeline import ScriptGenerationPipeline
from services.storyboard.fake_provider import FakeStoryboardDirector
from services.storyboard.openai_adapter import (
    OpenAIStoryboardConfig,
    OpenAIStoryboardDirector,
)
from services.storyboard.pipeline import StoryboardPipeline
from services.storyboard.providers import StoryboardDirector
from services.subtitles.acquisition import TranscriptAcquisitionService
from services.subtitles.opensubtitles import OpenSubtitlesAdapter
from services.subtitles.pipeline import SubtitlePipeline, SubtitlePipelineConfig
from services.transcription.fake import FakeTranscriptionProvider
from services.transcription.openai_adapter import OpenAITranscriptionAdapter
from services.transcription.pipeline import TranscriptionPipeline
from vidgen.contracts.media import ExtractedFrame, SceneBoundary
from vidgen.contracts.shot_workflow import (
    ProjectShotFanoutInput,
    ProjectShotFanoutResult,
    ResolveShotFanoutResult,
    ShotWorkflowIdentity,
    ShotWorkflowInput,
    ShotWorkflowProgress,
    ShotWorkflowResult,
    ShotWorkflowStatus,
)
from vidgen.contracts.transcription import TranscriptSegment, TranscriptWord
from vidgen.contracts.workflow import StageActivityInput, StageActivityResult
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.image_generation_models import GeneratedKeyframeImage, ImageGenerationRun
from vidgen.db.image_generation_repository import ImageGenerationRepository
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
    """Build configured T05-T13 adapters used by the production worker."""
    configured = settings or get_settings()
    return {
        "upload": _with_session(configured, _validate_upload),
        "media_processing": _with_session(configured, _process_media),
        "transcript_acquisition": _with_session(configured, _acquire_transcript),
        "evidence": _with_session(configured, _build_evidence),
        "episode_analysis": _with_session(configured, _analyze_episode),
        "script_generation": _with_session(configured, _generate_script),
        "narration": _with_session(configured, _generate_narration),
        "storyboard": _with_session(configured, _generate_storyboard),
        "image_generation": _with_session(configured, _generate_keyframes),
    }


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _shot_input(
    request: ProjectShotFanoutInput, selected: object, shot: object
) -> ShotWorkflowInput:
    # Kept local to the activity so mutable database/configuration reads never
    # occur in workflow code.
    storyboard = selected.storyboard
    material: dict[str, str | int] = {
        "project_id": str(request.project_id),
        "storyboard_run_id": str(storyboard.id),
        "storyboard_input_hash": storyboard.input_hash,
        "storyboard_shot_id": str(shot.stable_shot_id),
        "canonical_shot_hash": _canonical_hash(shot.contract),
        "shot_sequence": shot.global_sequence,
        "timing_manifest_hash": selected.timing_asset.sha256,
        "t14_configuration_identity": request.t14_configuration_identity,
        "t15_capability_profile_identity": request.t15_capability_profile_identity,
        "t14_pipeline_version": T14_PIPELINE_VERSION,
        "t15_pipeline_version": T15_PIPELINE_VERSION,
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": request.attempt_policy_version,
    }
    digest = identity_hash(material)
    identity = ShotWorkflowIdentity(**material, identity_hash=digest)
    return ShotWorkflowInput(
        project_id=request.project_id,
        storyboard_run_id=storyboard.id,
        storyboard_shot_id=shot.stable_shot_id,
        shot_input_hash=digest,
        workflow_identity=identity,
        idempotency_key=f"{request.idempotency_key}:{digest}",
        trace_context=request.trace_context,
        attempt_policy_version=request.attempt_policy_version,
    )


def _resolve_shot_fanout(
    session: Session,
    _blob_store: FilesystemBlobStore,
    settings: APISettings,
    image_provider: ImageGenerationProvider,
    video_provider: VideoGenerationProvider,
    raw_request: object,
) -> ResolveShotFanoutResult:
    request = ProjectShotFanoutInput.model_validate(raw_request)
    selected = ImageGenerationRepository(session).selected_storyboard(
        request.project_id, request.storyboard_run_id
    )
    request = request.model_copy(
        update={
            "t14_configuration_identity": (
                f"{image_provider.name}:{settings.image_model}:image-provider/1"
            ),
            "t15_capability_profile_identity": (
                f"{video_provider.name}:{settings.visual_capability_profile}:runway/2024-11-06"
            ),
        }
    )
    return ResolveShotFanoutResult(
        shots=[_shot_input(request, selected, shot) for shot in selected.shots]
    )


def _authoritative_shot(session: Session, request: ShotWorkflowInput) -> tuple[object, object]:
    selected = ImageGenerationRepository(session).selected_storyboard(
        request.project_id, request.storyboard_run_id
    )
    shot = next(
        (row for row in selected.shots if row.stable_shot_id == request.storyboard_shot_id),
        None,
    )
    if shot is None:
        raise ValueError("InvalidLineage: shot is not part of selected storyboard")
    fanout = ProjectShotFanoutInput(
        project_id=request.project_id,
        storyboard_run_id=request.storyboard_run_id,
        idempotency_key=request.idempotency_key.rsplit(":", 1)[0],
        trace_context=request.trace_context,
        t14_configuration_identity=request.workflow_identity.t14_configuration_identity,
        t15_capability_profile_identity=(request.workflow_identity.t15_capability_profile_identity),
        attempt_policy_version=request.attempt_policy_version,
    )
    expected = _shot_input(fanout, selected, shot)
    if expected.workflow_identity != request.workflow_identity:
        raise ValueError("InvalidLineage: shot workflow identity is stale or incompatible")
    return selected, shot


def _resolve_shot_input(
    session: Session,
    _blob_store: FilesystemBlobStore,
    _settings: APISettings,
    _image_provider: ImageGenerationProvider,
    _video_provider: VideoGenerationProvider,
    raw_request: object,
) -> ShotWorkflowProgress:
    request = ShotWorkflowInput.model_validate(raw_request)
    _authoritative_shot(session, request)
    return ShotWorkflowProgress(
        state=ShotWorkflowStatus.PROMPTING,
        current_stage="authoritative_input_resolved",
        current_attempt=1,
        last_checkpoint="lineage_validated",
    )


def _run_shot_keyframe(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    image_provider: ImageGenerationProvider,
    _video_provider: VideoGenerationProvider,
    raw_request: object,
) -> ShotWorkflowProgress:
    request = ShotWorkflowInput.model_validate(raw_request)
    _authoritative_shot(session, request)
    result = asyncio.run(
        ImageGenerationPipeline(
            session,
            blob_store,
            image_provider,
            model=settings.image_model,
            provider_configuration_version=(request.workflow_identity.t14_configuration_identity),
            cancellation_check=activity.is_cancelled,
        ).process(
            project_id=request.project_id,
            storyboard_id=request.storyboard_run_id,
            shot_id=request.storyboard_shot_id,
            idempotency_key=shot_activity_idempotency_key(request.shot_input_hash, "t14"),
        )
    )
    first = next(
        (
            item.candidate
            for item in result.items
            if item.candidate is not None and item.keyframe_role.value == "FIRST_FRAME"
        ),
        None,
    )
    if first is None:
        raise RuntimeError("TechnicalValidationFailure: T14 did not select a first keyframe")
    return ShotWorkflowProgress(
        state=ShotWorkflowStatus.KEYFRAME_QA,
        current_stage="t14_complete",
        current_attempt=1,
        t14_run_id=result.run_id,
        selected_keyframe_asset_id=first.asset_id,
        last_checkpoint="selected_keyframe_persisted",
    )


def _run_shot_animation(
    session: Session,
    blob_store: FilesystemBlobStore,
    _settings: APISettings,
    _image_provider: ImageGenerationProvider,
    video_provider: VideoGenerationProvider,
    raw_request: object,
) -> ShotWorkflowResult:
    request = ShotWorkflowInput.model_validate(raw_request)
    _, shot = _authoritative_shot(session, request)
    image_run = session.scalar(
        select(ImageGenerationRun).where(
            ImageGenerationRun.project_id == request.project_id,
            ImageGenerationRun.idempotency_key
            == shot_activity_idempotency_key(request.shot_input_hash, "t14"),
        )
    )
    if image_run is None or image_run.status != "keyframes_complete":
        raise ValueError("InvalidLineage: compatible completed T14 run is missing")
    result = asyncio.run(
        AnimationPipeline(
            session,
            blob_store,
            video_provider,
            provider_configuration_version=(
                request.workflow_identity.t15_capability_profile_identity
            ),
            cancellation_check=activity.is_cancelled,
        ).process(
            project_id=request.project_id,
            storyboard_id=request.storyboard_run_id,
            image_run_id=image_run.id,
            shot_id=request.storyboard_shot_id,
            idempotency_key=shot_activity_idempotency_key(request.shot_input_hash, "t15"),
        )
    )
    item_result = result.items[0] if result.items else None
    candidate = item_result.candidate if item_result is not None else None
    if candidate is None:
        raise RuntimeError("TechnicalValidationFailure: T15 did not select a canonical clip")
    video = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.canonical_asset_id == candidate.canonical_asset_id
        )
    )
    if video is None:
        raise RuntimeError("TechnicalValidationFailure: selected T15 clip is not durable")
    keyframe = session.scalar(
        select(GeneratedKeyframeImage).where(
            GeneratedKeyframeImage.shot_id == shot.id,
            GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
            GeneratedKeyframeImage.selected,
        )
    )
    return ShotWorkflowResult(
        shot_id=request.storyboard_shot_id,
        child_workflow_id="",  # workflow replaces this compact deterministic reference
        identity_hash=request.shot_input_hash,
        final_state=ShotWorkflowStatus.VIDEO_QA,
        t14_run_id=image_run.id,
        selected_keyframe_asset_id=keyframe.asset_id if keyframe else None,
        t15_run_id=result.run_id,
        selected_video_asset_id=video.canonical_asset_id,
        exact_usable_duration_us=shot.usable_duration_us,
        provider_generation_duration_us=round(video.provider_duration * 1_000_000),
        warning_codes=[str(item.get("code")) for item in video.trim_manifest.get("warnings", [])],
    )


def _persist_fanout_status(settings: APISettings) -> ShotActivityHandler:
    engine = build_engine(settings.database_url)

    def persist(raw_request: object) -> ProjectShotFanoutResult:
        request = ProjectShotFanoutResult.model_validate(raw_request)
        with Session(engine) as session:
            project = session.get(Project, request.project_id)
            if project is None:
                raise ValueError("InvalidLineage: project does not exist")
            project.status = request.status
            session.commit()
        return request

    return persist


def build_shot_production_handlers(
    settings: APISettings | None = None,
) -> dict[str, ShotActivityHandler]:
    """Build T16 adapters while retaining T14/T15 ownership of paid operations."""
    configured = settings or get_settings()
    engine = build_engine(configured.database_url)
    blob_store = FilesystemBlobStore(configured.blob_root, configured.signing_secret.encode())
    image_provider: ImageGenerationProvider
    video_provider: VideoGenerationProvider
    if configured.openai_api_key:
        from openai import OpenAI

        image_provider = OpenAIImageProvider(
            OpenAI(api_key=configured.openai_api_key, max_retries=0)
        )
    elif configured.temporal_allow_fake_providers:
        image_provider = DeterministicFakeImageProvider()
    else:
        raise ValueError("T16 image generation provider is not configured")
    if configured.runway_api_secret:
        from runwayml import AsyncRunwayML

        video_provider = RunwayVideoProvider(
            AsyncRunwayML(api_key=configured.runway_api_secret, max_retries=0)
        )
    elif configured.temporal_allow_fake_providers:
        # Retain one fake instance so a retried polling activity can retrieve the
        # deterministic task submitted by the prior activity execution.
        video_provider = FakeVideoProvider()
    else:
        raise ValueError("T16 video generation provider is not configured")

    def with_session(handler: Callable[..., object]) -> ShotActivityHandler:
        def execute(request: object) -> object:
            with Session(engine, expire_on_commit=False) as session:
                return handler(
                    session, blob_store, configured, image_provider, video_provider, request
                )

        return execute

    return {
        "resolve_shot_fanout": with_session(_resolve_shot_fanout),
        "resolve_shot_input": with_session(_resolve_shot_input),
        "run_shot_keyframe": with_session(_run_shot_keyframe),
        "run_shot_animation": with_session(_run_shot_animation),
        # T14/T15 rows are the durable shot checkpoints; these activities form
        # an explicit commit/query boundary without duplicating their tables.
        "persist_shot_checkpoint": lambda request: request,
        "persist_shot_fanout_checkpoint": _persist_fanout_status(configured),
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
    provider: NarrationProvider
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


def _generate_storyboard(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    """T13. Only IDs cross the workflow boundary; payloads stay in storage."""
    director: StoryboardDirector
    if settings.openai_api_key:
        director = OpenAIStoryboardDirector(
            OpenAIStoryboardConfig(api_key=settings.openai_api_key, model=settings.storyboard_model)
        )
    elif settings.temporal_allow_fake_providers:
        director = FakeStoryboardDirector()
    else:
        raise ValueError("storyboard director is not configured")
    result = asyncio.run(
        StoryboardPipeline(
            session,
            blob_store,
            director,
            capability_profile_id=settings.visual_capability_profile,
            cancellation_check=activity.is_cancelled,
        ).process(
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
        )
    )
    return StageActivityResult(
        stage=request.stage,
        entity_id=result.storyboard_run_id,
        asset_id=result.storyboard_asset_id,
        reused=False,
    )


def _generate_keyframes(
    session: Session,
    blob_store: FilesystemBlobStore,
    settings: APISettings,
    request: StageActivityInput,
) -> StageActivityResult:
    """T14. The activity receives IDs only and resumes database checkpoints."""
    provider: ImageGenerationProvider
    if settings.openai_api_key:
        from openai import OpenAI

        provider = OpenAIImageProvider(OpenAI(api_key=settings.openai_api_key, max_retries=0))
    elif settings.temporal_allow_fake_providers:
        provider = DeterministicFakeImageProvider()
    else:
        raise ValueError("image generation provider is not configured")
    result = asyncio.run(
        ImageGenerationPipeline(
            session,
            blob_store,
            provider,
            model=settings.image_model,
            cancellation_check=activity.is_cancelled,
        ).process(project_id=request.project_id, idempotency_key=request.idempotency_key)
    )
    asset_id = next(
        (item.candidate.asset_id for item in result.items if item.candidate is not None), None
    )
    return StageActivityResult(
        stage=request.stage,
        entity_id=result.run_id,
        asset_id=asset_id,
        reused=result.reused_count == result.requested_count,
    )
