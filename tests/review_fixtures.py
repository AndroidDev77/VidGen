"""A complete, deterministic T01-T17 project graph for the T18 review-UI tests.

The graph is built directly from the persisted models with synthetic media and
fake providers, so no test in this suite makes a paid provider call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.cost_models import (
    CostLedgerEntry,
    PipelineFailureEvent,
    PricingVersion,
    ProjectBudget,
    ProviderAttempt,
)
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord, EpisodeAnalysisRun
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.models import Asset, Project, RenderJob, SourceVideo
from vidgen.db.narration_models import NarrationRun, NarrationSegment, VoiceProfileRecord
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.script_models import (
    CompressedPlotPlanRecord,
    Script,
    ScriptGenerationRun,
    ScriptSegment,
)
from vidgen.db.storyboard_models import (
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.transcription_models import (
    Transcript,
    TranscriptionRun,
    TranscriptSegmentRecord,
)
from vidgen.db.upload_models import UploadSession
from vidgen.db.workflow_models import EvidencePackageRecord

SHOT_COUNT = 10
SHOT_DURATION_US = 3_000_000
GENERATION_DURATION_US = 4_000_000


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(slots=True)
class ProjectGraph:
    """Identifiers the T18 tests assert against."""

    project_id: UUID
    source_video_id: UUID
    transcript_id: UUID
    transcript_segment_ids: list[UUID]
    script_id: UUID
    script_segment_ids: list[UUID]
    storyboard_run_id: UUID
    shot_ids: list[UUID] = field(default_factory=list)
    video_attempt_ids: list[UUID] = field(default_factory=list)
    keyframe_asset_ids: list[UUID] = field(default_factory=list)
    render_job_id: UUID | None = None
    final_video_asset_id: UUID | None = None
    srt_asset_id: UUID | None = None
    webvtt_asset_id: UUID | None = None


def _asset(
    session: Session,
    project_id: UUID,
    kind: str,
    name: str,
    media_type: str,
    blob_root: Path | None = None,
) -> Asset:
    key = f"blobs/{digest(f'{project_id}:{name}')[:8]}/{name}"
    payload = f"synthetic:{project_id}:{name}".encode()
    if blob_root is not None:
        target = blob_root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    asset = Asset(
        project_id=project_id,
        kind=kind,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        media_type=media_type,
        storage_key=key,
    )
    session.add(asset)
    session.flush()
    return asset


def build_project_graph(
    session: Session,
    *,
    owner_subject: str = "local-user",
    name: str = "Season 3 Episode 4",
    with_render: bool = True,
    blob_root: Path | None = None,
) -> ProjectGraph:
    """Create one fully populated project owned by ``owner_subject``."""
    project = Project(
        name=name,
        owner_subject=owner_subject,
        status="review",
        target_duration_seconds=300,
        visual_style="flat editorial cartoon",
        humor_intensity=6,
        settings={},
    )
    session.add(project)
    session.flush()

    source_asset = _asset(session, project.id, "source_video", "source.mp4", "video/mp4", blob_root)
    source = SourceVideo(
        project_id=project.id,
        asset_id=source_asset.id,
        filename="episode.mp4",
        duration_seconds=1800.0,
        width=1920,
        height=1080,
        frame_rate=24.0,
        probe={},
    )
    session.add(source)
    session.add(
        UploadSession(
            project_id=project.id,
            owner_subject=owner_subject,
            filename="episode.mp4",
            media_type="video/mp4",
            expected_size=1024,
            expected_sha256=digest("source.mp4"),
            part_size=1024,
            status="completed",
            completed_asset_id=source_asset.id,
        )
    )
    session.flush()

    audio_asset = _asset(session, project.id, "audio", "audio.wav", "audio/wav", blob_root)
    transcript_asset = _asset(
        session, project.id, "json", "transcript.json", "application/json", blob_root
    )
    run = TranscriptionRun(
        project_id=project.id,
        source_video_id=source.id,
        source_audio_asset_id=audio_asset.id,
        idempotency_key="t03:1",
        status="completed",
        language="en",
        chunker_version="v1",
        provider="fake",
        transcription_model="fake-whisper",
        diarization_model="fake-diarize",
        parameters={},
        coverage_score=0.98,
        selected=True,
    )
    session.add(run)
    session.flush()
    transcript = Transcript(
        project_id=project.id,
        run_id=run.id,
        version=1,
        language="en",
        text="Line one. Line two. Line three.",
        transcript_asset_id=transcript_asset.id,
        duration_seconds=1800.0,
        coverage_score=0.98,
        selected=True,
        warnings=[],
    )
    session.add(transcript)
    session.flush()
    transcript_segments = []
    for index in range(3):
        segment = TranscriptSegmentRecord(
            transcript_id=transcript.id,
            sequence=index,
            start_seconds=float(index * 10),
            end_seconds=float(index * 10 + 9),
            text=f"Transcript line {index + 1}.",
            speaker_label=f"SPEAKER_{index % 2:02d}",
            confidence=0.9,
            source_chunk_ids=[],
            words=[],
            provenance={"provider": "fake"},
        )
        session.add(segment)
        transcript_segments.append(segment)
    session.flush()

    evidence_asset = _asset(
        session, project.id, "json", "evidence.json", "application/json", blob_root
    )
    evidence = EvidencePackageRecord(
        project_id=project.id,
        version=1,
        selected=True,
        input_hash=digest("evidence"),
        schema_version="1.0",
        source_video_id=source.id,
        source_video_asset_id=source_asset.id,
        source_audio_asset_id=audio_asset.id,
        transcript_id=transcript.id,
        transcript_asset_id=transcript_asset.id,
        transcript_origin="transcription",
        provenance={},
    )
    session.add(evidence)
    session.flush()
    _ = evidence_asset

    analysis_run = EpisodeAnalysisRun(
        project_id=project.id,
        source_video_id=source.id,
        evidence_package_id=evidence.id,
        idempotency_key="t10:1",
        input_hash=digest("analysis"),
        contract_version="1.0",
        prompt_version="v1",
        provider_configuration_version="fake/1",
        provider="fake",
        model="fake-analyst",
        status="completed",
        attempt_count=1,
        selected=True,
    )
    session.add(analysis_run)
    session.flush()
    analysis = EpisodeAnalysisRecord(
        project_id=project.id,
        analysis_run_id=analysis_run.id,
        version=1,
        canonical_analysis_asset_id=_asset(
            session, project.id, "json", "analysis.json", "application/json", blob_root
        ).id,
        input_hash=digest("analysis"),
        duration_ms=1_800_000,
        character_count=3,
        location_count=2,
        scene_count=8,
        plot_beat_count=6,
        selected=True,
        warnings=[],
    )
    session.add(analysis)
    session.flush()

    script_run = ScriptGenerationRun(
        project_id=project.id,
        episode_analysis_id=analysis.id,
        idempotency_key="t11:1",
        input_hash=digest("script"),
        status="completed",
        target_duration_ms=300_000,
        target_word_count=700,
        target_words_per_minute=140,
        humor_intensity=0.6,
        recap_mode="comedy",
        provider_configuration_version="fake/1",
        compressor_model="fake",
        writer_model="fake",
        editor_model="fake",
        compressor_prompt_version="v1",
        writer_prompt_version="v1",
        editor_prompt_version="v1",
        rubric_version="comedy_v1",
        attempt_count=1,
        revision_count=0,
    )
    session.add(script_run)
    session.flush()
    plan = CompressedPlotPlanRecord(
        project_id=project.id,
        generation_run_id=script_run.id,
        episode_analysis_id=analysis.id,
        version=1,
        input_hash=digest("plan"),
        canonical_plan_asset_id=_asset(
            session, project.id, "json", "plan.json", "application/json", blob_root
        ).id,
        selected_beat_count=6,
        omitted_beat_count=1,
        target_word_count=700,
        validation_report={},
        selected=True,
    )
    session.add(plan)
    session.flush()
    script = Script(
        project_id=project.id,
        generation_run_id=script_run.id,
        episode_analysis_id=analysis.id,
        compressed_plot_plan_id=plan.id,
        version=1,
        status="approved",
        target_word_count=700,
        actual_word_count=60,
        target_duration_ms=300_000,
        humor_intensity=0.6,
        canonical_script_asset_id=_asset(
            session, project.id, "json", "script.json", "application/json", blob_root
        ).id,
        prompt_version="v1",
        rubric_version="comedy_v1",
        review_scores={},
        selected=True,
    )
    session.add(script)
    session.flush()
    script_segments: list[ScriptSegment] = []
    for index in range(SHOT_COUNT):
        segment = ScriptSegment(
            script_id=script.id,
            sequence=index,
            stable_segment_id=uuid4(),
            segment_type="narration",
            speaker_kind="narrator",
            anonymous_speaker_label=None,
            text=f"Recap beat number {index + 1} lands with a joke.",
            content_hash=digest(f"segment-{index}"),
            plot_beat_ids=[],
            source_scene_ids=[],
            joke_annotations=[],
            visual_gag=None,
            estimated_duration_ms=3_000,
            voice_direction="",
            locked=False,
        )
        session.add(segment)
        script_segments.append(segment)
    session.flush()

    voice = VoiceProfileRecord(
        project_id=project.id,
        provider="fake",
        provider_voice_id="fake-voice",
        model="fake-tts",
        language="en",
        version=1,
        configuration={},
        configuration_hash=digest("voice"),
    )
    session.add(voice)
    session.flush()
    narration = NarrationRun(
        project_id=project.id,
        script_id=script.id,
        script_version=script.version,
        voice_profile_id=voice.id,
        voice_profile_version=1,
        idempotency_key="t12:1",
        input_hash=digest("narration"),
        status="completed",
        pipeline_version="t12/1",
        selected=True,
        total_duration_seconds=30.0,
        parameters={},
    )
    session.add(narration)
    session.flush()
    narration_segments: list[NarrationSegment] = []
    for index, segment in enumerate(script_segments):
        row = NarrationSegment(
            narration_run_id=narration.id,
            script_segment_id=segment.id,
            sequence=index,
            text_hash=digest(f"narration-{index}"),
            generation_identity=digest(f"{project.id}:narration-identity-{index}"),
            status="completed",
            duration_seconds=3.0,
        )
        session.add(row)
        narration_segments.append(row)
    session.flush()

    storyboard = StoryboardRun(
        project_id=project.id,
        episode_model_id=analysis.id,
        script_id=script.id,
        script_version=script.version,
        narration_run_id=narration.id,
        capability_profile_id="runway-gen4-turbo",
        capability_hash=digest("capability"),
        idempotency_key="t13:1",
        input_hash=digest("storyboard"),
        status="completed",
        provider="fake",
        model="fake-director",
        contract_version="1.0",
        director_version="v1",
        prompt_version="v1",
        retimer_version="v1",
        version=1,
        selected=True,
        storyboard_asset_id=_asset(
            session, project.id, "json", "storyboard.json", "application/json", blob_root
        ).id,
        timing_manifest_asset_id=_asset(
            session, project.id, "json", "timing.json", "application/json", blob_root
        ).id,
        segment_count=SHOT_COUNT,
        shot_count=SHOT_COUNT,
        total_duration_us=SHOT_COUNT * SHOT_DURATION_US,
        parameters={},
    )
    session.add(storyboard)
    session.flush()

    image_run = ImageGenerationRun(
        project_id=project.id,
        storyboard_id=storyboard.id,
        storyboard_version=storyboard.version,
        idempotency_key="t14:1",
        input_hash=digest("images"),
        status="completed",
        provider_configuration_version="fake/1",
        prompt_compiler_version="v1",
        pipeline_version="t14/1",
        requested_item_count=SHOT_COUNT,
        completed_item_count=SHOT_COUNT,
        failed_item_count=0,
        parameters={},
    )
    session.add(image_run)
    session.flush()
    animation_run = AnimationRun(
        project_id=project.id,
        storyboard_id=storyboard.id,
        storyboard_version=storyboard.version,
        image_generation_run_id=image_run.id,
        idempotency_key="t15:1",
        input_hash=digest("animation"),
        status="completed",
        routing_policy_version="v1",
        provider_configuration_version="fake/1",
        pipeline_version="t15/1",
        requested_item_count=SHOT_COUNT,
        completed_item_count=SHOT_COUNT,
        failed_item_count=0,
        original_video_count=SHOT_COUNT,
        canonical_video_count=SHOT_COUNT,
        parameters={},
    )
    session.add(animation_run)
    session.flush()

    pricing = PricingVersion(
        name=f"t18-test-{project.id}",
        currency="USD",
        source_metadata={},
        verification_date=datetime.now(UTC).date(),
        activated_at=datetime.now(UTC),
    )
    session.add(pricing)
    session.add(
        ProjectBudget(
            project_id=project.id,
            warning_cap=Decimal("8.000000"),
            hard_cap=Decimal("20.000000"),
            currency="USD",
            policy_version="v1",
            reserved_amount=Decimal("1.000000"),
            committed_amount=Decimal("0"),
            released_amount=Decimal("0"),
        )
    )
    session.flush()

    graph = ProjectGraph(
        project_id=project.id,
        source_video_id=source.id,
        transcript_id=transcript.id,
        transcript_segment_ids=[row.id for row in transcript_segments],
        script_id=script.id,
        script_segment_ids=[row.id for row in script_segments],
        storyboard_run_id=storyboard.id,
    )

    for index in range(SHOT_COUNT):
        checkpoint = StoryboardSegmentCheckpoint(
            storyboard_run_id=storyboard.id,
            script_segment_id=script_segments[index].id,
            narration_segment_id=narration_segments[index].id,
            sequence=index,
            input_hash=digest(f"checkpoint-{index}"),
            idempotency_key=f"t13:checkpoint:{project.id}:{index}",
            status="completed",
            attempt_count=1,
            repair_attempt_count=0,
            narration_duration_us=SHOT_DURATION_US,
            global_start_us=index * SHOT_DURATION_US,
        )
        session.add(checkpoint)
        session.flush()
        shot = StoryboardShotRecord(
            storyboard_run_id=storyboard.id,
            segment_checkpoint_id=checkpoint.id,
            stable_shot_id=uuid4(),
            global_sequence=index,
            segment_sequence=0,
            script_segment_id=script_segments[index].id,
            narration_segment_id=narration_segments[index].id,
            start_us=0,
            end_us=SHOT_DURATION_US,
            global_start_us=index * SHOT_DURATION_US,
            global_end_us=(index + 1) * SHOT_DURATION_US,
            usable_duration_us=SHOT_DURATION_US,
            requested_generation_duration_us=GENERATION_DURATION_US,
            trim_start_us=0,
            trim_end_us=GENERATION_DURATION_US - SHOT_DURATION_US,
            transition_handle_us=0,
            word_start_index=index * 6,
            word_end_index=index * 6 + 6,
            camera={"framing": "medium", "angle": "eye_level", "movement": "static"},
            action={"subject_action": f"Beat {index + 1}"},
            transition_in={"kind": "cut"},
            transition_out={"kind": "cut"},
            references={"character_reference_ids": ["protagonist"], "location_reference_id": "set"},
            incoming_continuity={},
            outgoing_continuity={},
            contract={
                "visual_objective": f"Show beat {index + 1} in a wide comic frame.",
                "evidence_references": [{"source_asset_id": str(source_asset.id)}],
            },
            provenance={},
        )
        session.add(shot)
        session.flush()
        graph.shot_ids.append(shot.id)

        keyframe_asset = _asset(
            session, project.id, "image", f"keyframe-{index}.png", "image/png", blob_root
        )
        image_item = ImageGenerationItem(
            run_id=image_run.id,
            shot_id=shot.id,
            shot_sequence=index,
            keyframe_role="FIRST_FRAME",
            generation_identity=digest(f"{project.id}:image-identity-{index}"),
            input_hash=digest(f"image-input-{index}"),
            prompt_package={},
            status="completed",
            attempt_count=1,
        )
        session.add(image_item)
        session.flush()
        session.add(
            GeneratedKeyframeImage(
                project_id=project.id,
                shot_id=shot.id,
                keyframe_role="FIRST_FRAME",
                item_id=image_item.id,
                asset_id=keyframe_asset.id,
                provider="fake",
                model="fake-image",
                prompt_hash=digest(f"prompt-{index}"),
                reference_hash=digest(f"reference-{index}"),
                width=1280,
                height=720,
                mime_type="image/png",
                byte_size=2048,
                sha256=keyframe_asset.sha256,
                validation_report={},
                selected=True,
            )
        )
        graph.keyframe_asset_ids.append(keyframe_asset.id)

        item = AnimationItem(
            run_id=animation_run.id,
            shot_id=shot.id,
            shot_sequence=index,
            first_keyframe_asset_id=keyframe_asset.id,
            generation_identity=digest(f"{project.id}:animation-identity-{index}"),
            motion_prompt_hash=digest(f"motion-{index}"),
            motion_prompt_package={},
            provider="fake",
            model="fake-video",
            requested_duration=GENERATION_DURATION_US / 1_000_000,
            width=1280,
            height=720,
            status="locked",
            attempt_count=1,
            warnings=[],
        )
        session.add(item)
        session.flush()
        attempt = ProviderAttempt(
            project_id=project.id,
            related_entity_type="storyboard_shot",
            related_entity_id=shot.id,
            operation="video_generation",
            attempt_number=1,
            input_hash=digest(f"attempt-{index}"),
            idempotency_key=f"t15:attempt:{project.id}:{index}",
            provider="fake",
            model="fake-video",
            provider_request_id=f"{project.id}-task-{index}",
            provider_configuration_version="fake/1",
            status="succeeded",
            started_at=datetime.now(UTC),
        )
        session.add(attempt)
        session.flush()
        session.add(
            CostLedgerEntry(
                project_id=project.id,
                provider_attempt_id=attempt.id,
                provider="fake",
                model="fake-video",
                operation="video_generation",
                reason="generation",
                currency="USD",
                estimated_amount=Decimal("0.100000"),
                reserved_amount=Decimal("0.100000"),
                actual_amount=Decimal("0.100000"),
                released_amount=Decimal("0"),
                usage=[],
                status="reconciled",
                idempotency_key=f"t23:ledger:{project.id}:{index}",
            )
        )
        original = _asset(
            session, project.id, "video", f"shot-{index}-raw.mp4", "video/mp4", blob_root
        )
        canonical = _asset(
            session, project.id, "video", f"shot-{index}.mp4", "video/mp4", blob_root
        )
        video = AnimationGeneratedVideo(
            project_id=project.id,
            shot_id=shot.id,
            animation_item_id=item.id,
            provider_attempt_id=attempt.id,
            remote_task_id=f"{project.id}-task-{index}",
            original_asset_id=original.id,
            canonical_asset_id=canonical.id,
            requested_duration=GENERATION_DURATION_US / 1_000_000,
            provider_duration=GENERATION_DURATION_US / 1_000_000,
            canonical_duration=SHOT_DURATION_US / 1_000_000,
            width=1280,
            height=720,
            codec="h264",
            container="mp4",
            frame_rate="24/1",
            sha256=canonical.sha256,
            validation_report={},
            trim_manifest={},
            selected=True,
        )
        session.add(video)
        session.flush()
        item.selected_generated_video_id = video.id
        graph.video_attempt_ids.append(video.id)
    session.flush()

    session.add(
        PipelineFailureEvent(
            project_id=project.id,
            workflow_id=f"vidgen-project-{project.id}",
            stage="animation",
            failure_class="transient",
            error_code="provider_timeout",
            retryable=True,
            event_version="pipeline.failure.v1",
            idempotency_key=f"t23:failure:{project.id}:1",
            projected_status="recovered",
            diagnostics={},
        )
    )

    if with_render:
        _add_render(session, project, storyboard, script, narration, graph, blob_root)
    session.commit()
    return graph


def _add_render(
    session: Session,
    project: Project,
    storyboard: StoryboardRun,
    script: Script,
    narration: NarrationRun,
    graph: ProjectGraph,
    blob_root: Path | None = None,
) -> None:
    manifest = _asset(session, project.id, "json", "manifest.json", "application/json", blob_root)
    srt = _asset(session, project.id, "subtitle", "captions.srt", "application/x-subrip", blob_root)
    webvtt = _asset(session, project.id, "subtitle", "captions.vtt", "text/vtt", blob_root)
    report = _asset(session, project.id, "json", "verification.json", "application/json", blob_root)
    final = _asset(session, project.id, "render", "final.mp4", "video/mp4", blob_root)
    premaster = _asset(session, project.id, "audio", "premaster.wav", "audio/wav", blob_root)
    render = RenderJob(
        project_id=project.id,
        status="render_complete",
        attempt=1,
        manifest_asset_id=manifest.id,
        output_asset_id=final.id,
        error={"warnings": []},
        script_id=script.id,
        script_version=script.version,
        narration_run_id=narration.id,
        storyboard_run_id=storyboard.id,
        render_identity=digest(f"{project.id}:render-identity"),
        idempotency_key="t17:1",
        input_hash=digest("render-input"),
        srt_asset_id=srt.id,
        webvtt_asset_id=webvtt.id,
        premaster_audio_asset_id=premaster.id,
        final_video_asset_id=final.id,
        verification_report_asset_id=report.id,
        expected_duration_us=SHOT_COUNT * SHOT_DURATION_US,
        measured_duration_us=SHOT_COUNT * SHOT_DURATION_US,
        video_profile={"width": 1280, "height": 720},
        audio_profile={"integrated_loudness_lufs": -16.0, "true_peak_dbtp": -1.5},
        caption_profile={"mode": "external"},
        ffmpeg_version="ffmpeg-test",
        pipeline_version="t17/1",
        selected=True,
        completed_at=datetime.now(UTC),
    )
    session.add(render)
    session.flush()
    session.add(
        CaptionTrackRecord(
            render_job_id=render.id,
            narration_run_id=narration.id,
            caption_identity=digest(f"{project.id}:caption-identity"),
            language="en",
            cue_count=12,
            start_us=0,
            end_us=SHOT_COUNT * SHOT_DURATION_US,
            srt_asset_id=srt.id,
            webvtt_asset_id=webvtt.id,
            validation_report_asset_id=report.id,
            configuration_hash=digest("caption-config"),
        )
    )
    session.flush()
    graph.render_job_id = render.id
    graph.final_video_asset_id = final.id
    graph.srt_asset_id = srt.id
    graph.webvtt_asset_id = webvtt.id
