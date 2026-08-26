"""Deterministic T13 fixtures derived from fake narration metadata.

No media files are committed: durations and word timings are the same integer
microsecond values a measured T12 run persists after ffprobe.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import vidgen.db.storyboard_models
import vidgen.db.workflow_models  # noqa: F401
from services.storyboard.canonicalize import canonical_json, seconds_to_us
from vidgen.db.base import Base
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord, EpisodeAnalysisRun
from vidgen.db.models import Project
from vidgen.db.narration_models import NarrationRun, NarrationSegment, VoiceProfileRecord
from vidgen.db.script_models import (
    CompressedPlotPlanRecord,
    Script,
    ScriptGenerationRun,
    ScriptSegment,
)
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore

WORDS_PER_SECOND = 2  # 500 ms per word keeps every fixture duration exact.


@dataclass(slots=True)
class StoryboardFixture:
    session: Session
    blobs: FilesystemBlobStore
    project: Project
    episode_model: EpisodeAnalysisRecord
    script: Script
    script_segments: list[ScriptSegment]
    narration_run: NarrationRun
    narration_segments: list[NarrationSegment]
    evidence_package: EvidencePackageRecord
    scene_evidence_ids: list[UUID]
    character_ids: list[UUID]
    location_ids: list[UUID]


def word_timings(text: str, *, seconds_per_word: float = 0.5) -> list[dict[str, Any]]:
    """Measured word timings with punctuation preserved, exactly as T12 persists."""
    timings: list[dict[str, Any]] = []
    cursor = 0.0
    for index, raw in enumerate(text.split()):
        stripped = raw.rstrip(".,!?;:")
        punctuation = raw[len(stripped) :]
        timings.append(
            {
                "schema_version": "1.0",
                "word_index": index,
                "word": raw,
                "comparison_token": stripped.lower(),
                "punctuation": punctuation,
                "start_seconds": round(cursor, 6),
                "end_seconds": round(cursor + seconds_per_word, 6),
                "confidence": 1.0,
            }
        )
        cursor += seconds_per_word
    return timings


def segment_duration_us(text: str, *, seconds_per_word: float = 0.5) -> int:
    return seconds_to_us(round(len(text.split()) * seconds_per_word, 6))


DEFAULT_TEXTS = (
    "Our hero wakes up late, again, and the toaster is already on fire.",
    "He sprints for the bus, drops the toast, and the dog wins breakfast.",
)


def build_fixture(
    tmp_path: Path,
    *,
    texts: tuple[str, ...] = DEFAULT_TEXTS,
    seconds_per_word: float = 0.5,
    character_count: int = 2,
    anonymous_segments: frozenset[int] = frozenset(),
    joke_annotations: dict[int, list[dict[str, Any]]] | None = None,
    project_settings: dict[str, Any] | None = None,
    database_name: str = "storyboard.db",
) -> StoryboardFixture:
    engine = create_engine(f"sqlite:///{tmp_path / database_name}")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    blobs = FilesystemBlobStore(tmp_path / "blobs", b"storyboard-secret")
    assets = AssetService(session, blobs)

    project = Project(
        name="storyboard fixture",
        visual_style="flat 2d",
        status="narration_complete",
        target_duration_seconds=300,
        settings=project_settings or {},
    )
    session.add(project)
    session.flush()

    evidence, scene_ids = _evidence(session, assets, project.id)
    character_ids = [uuid4() for _ in range(character_count)]
    location_ids = [uuid4()]
    episode_model = _episode_model(
        session, assets, project.id, evidence.id, scene_ids, character_ids, location_ids
    )
    script, script_segments = _script(
        session,
        assets,
        project.id,
        episode_model,
        texts,
        scene_ids,
        anonymous_segments,
        joke_annotations or {},
    )
    narration_run, narration_segments = _narration(
        session, assets, project.id, script, script_segments, texts, seconds_per_word
    )
    session.commit()
    return StoryboardFixture(
        session=session,
        blobs=blobs,
        project=project,
        episode_model=episode_model,
        script=script,
        script_segments=script_segments,
        narration_run=narration_run,
        narration_segments=narration_segments,
        evidence_package=evidence,
        scene_evidence_ids=scene_ids,
        character_ids=character_ids,
        location_ids=location_ids,
    )


def _evidence(
    session: Session, assets: AssetService, project_id: UUID
) -> tuple[EvidencePackageRecord, list[UUID]]:
    source_asset = assets.store(
        content=b"fixture-source-video",
        kind="source_video",
        media_type="video/mp4",
        project_id=project_id,
    )
    transcript_asset = assets.store(
        content=b"fixture-transcript",
        kind="json",
        media_type="application/json",
        project_id=project_id,
    )
    from vidgen.db.models import SourceVideo

    source = SourceVideo(
        project_id=project_id,
        asset_id=source_asset.id,
        filename="fixture.mp4",
        duration_seconds=900.0,
    )
    session.add(source)
    session.flush()
    package = EvidencePackageRecord(
        project_id=project_id,
        version=1,
        selected=True,
        input_hash="e" * 64,
        schema_version="1.0",
        source_video_id=source.id,
        source_video_asset_id=source_asset.id,
        transcript_id=uuid4(),
        transcript_asset_id=transcript_asset.id,
        transcript_origin="subtitle",
        provenance={},
    )
    session.add(package)
    session.flush()
    scene_ids: list[UUID] = []
    for index in range(3):
        scene = SceneEvidenceRecord(
            evidence_package_id=package.id,
            scene_sequence=index,
            source_start_seconds=float(index * 10),
            source_end_seconds=float(index * 10 + 9),
            frame_asset_ids=[],
            evidence={},
        )
        session.add(scene)
        session.flush()
        scene_ids.append(scene.id)
    return package, scene_ids


def _episode_model(
    session: Session,
    assets: AssetService,
    project_id: UUID,
    evidence_package_id: UUID,
    scene_ids: list[UUID],
    character_ids: list[UUID],
    location_ids: list[UUID],
) -> EpisodeAnalysisRecord:
    source_video_id = uuid4()
    payload = {
        "schema_version": "1.0",
        "episode_id": str(uuid4()),
        "project_id": str(project_id),
        "characters": [
            {"character_id": str(item), "canonical_name": f"Character {index}", "anonymous": False}
            for index, item in enumerate(character_ids)
        ],
        "locations": [
            {"location_id": str(item), "canonical_name": f"Location {index}"}
            for index, item in enumerate(location_ids)
        ],
        "scenes": [{"scene_id": str(item)} for item in scene_ids],
    }
    analysis_asset = assets.store(
        content=canonical_json(payload).encode(),
        kind="json",
        media_type="application/vnd.vidgen.episode-analysis+json",
        project_id=project_id,
    )
    run = EpisodeAnalysisRun(
        project_id=project_id,
        source_video_id=source_video_id,
        evidence_package_id=evidence_package_id,
        idempotency_key="fixture-analysis",
        input_hash="a" * 64,
        contract_version="1.0",
        prompt_version="v1",
        provider_configuration_version="v1",
        provider="fake",
        model="fake-analyst",
        status="analysis_complete",
        selected=True,
    )
    session.add(run)
    session.flush()
    record = EpisodeAnalysisRecord(
        project_id=project_id,
        analysis_run_id=run.id,
        version=1,
        canonical_analysis_asset_id=analysis_asset.id,
        input_hash="a" * 64,
        duration_ms=900_000,
        character_count=len(character_ids),
        location_count=len(location_ids),
        scene_count=len(scene_ids),
        plot_beat_count=3,
        selected=True,
    )
    session.add(record)
    session.flush()
    return record


def _script(
    session: Session,
    assets: AssetService,
    project_id: UUID,
    episode_model: EpisodeAnalysisRecord,
    texts: tuple[str, ...],
    scene_ids: list[UUID],
    anonymous_segments: frozenset[int],
    joke_annotations: dict[int, list[dict[str, Any]]],
) -> tuple[Script, list[ScriptSegment]]:
    generation_run = ScriptGenerationRun(
        project_id=project_id,
        episode_analysis_id=episode_model.id,
        idempotency_key="fixture-script",
        input_hash="b" * 64,
        status="script_complete",
        target_duration_ms=300_000,
        target_word_count=750,
        target_words_per_minute=150,
        humor_intensity=0.6,
        recap_mode="full_recap",
        provider_configuration_version="v1",
        compressor_model="fake",
        writer_model="fake",
        editor_model="fake",
        compressor_prompt_version="v1",
        writer_prompt_version="v1",
        editor_prompt_version="v1",
        rubric_version="v1",
    )
    session.add(generation_run)
    session.flush()
    plan_asset = assets.store(
        content=b"fixture-plot-plan",
        kind="json",
        media_type="application/json",
        project_id=project_id,
    )
    plan = CompressedPlotPlanRecord(
        project_id=project_id,
        generation_run_id=generation_run.id,
        episode_analysis_id=episode_model.id,
        version=1,
        input_hash="c" * 64,
        canonical_plan_asset_id=plan_asset.id,
        selected_beat_count=3,
        omitted_beat_count=0,
        target_word_count=750,
        validation_report={},
        selected=True,
    )
    session.add(plan)
    session.flush()
    script_asset = assets.store(
        content=canonical_json({"segments": list(texts)}).encode(),
        kind="json",
        media_type="application/vnd.vidgen.script+json",
        project_id=project_id,
    )
    script = Script(
        project_id=project_id,
        generation_run_id=generation_run.id,
        episode_analysis_id=episode_model.id,
        compressed_plot_plan_id=plan.id,
        version=1,
        status="approved",
        target_word_count=750,
        actual_word_count=sum(len(text.split()) for text in texts),
        target_duration_ms=300_000,
        humor_intensity=0.6,
        canonical_script_asset_id=script_asset.id,
        prompt_version="v1",
        selected=True,
    )
    session.add(script)
    session.flush()
    segments: list[ScriptSegment] = []
    for index, text in enumerate(texts):
        anonymous = index in anonymous_segments
        segment = ScriptSegment(
            script_id=script.id,
            sequence=index,
            stable_segment_id=uuid4(),
            segment_type="narration",
            speaker_kind="anonymous" if anonymous else "narrator",
            anonymous_speaker_label="Speaker 1" if anonymous else None,
            text=text,
            content_hash=f"{index:064d}",
            plot_beat_ids=[],
            source_scene_ids=[str(scene_ids[index % len(scene_ids)])],
            joke_annotations=joke_annotations.get(index, []),
            estimated_duration_ms=max(1, len(text.split()) * 500),
            voice_direction="",
        )
        session.add(segment)
        session.flush()
        segments.append(segment)
    return script, segments


def _narration(
    session: Session,
    assets: AssetService,
    project_id: UUID,
    script: Script,
    script_segments: list[ScriptSegment],
    texts: tuple[str, ...],
    seconds_per_word: float,
) -> tuple[NarrationRun, list[NarrationSegment]]:
    profile = VoiceProfileRecord(
        project_id=project_id,
        provider="fake",
        provider_voice_id="cedar",
        model="fake-tts-1",
        language="en",
        version=1,
        configuration={},
        configuration_hash="d" * 64,
    )
    session.add(profile)
    session.flush()
    preview_asset = assets.store(
        content=b"fixture-preview-audio",
        kind="audio",
        media_type="audio/wav",
        project_id=project_id,
    )
    run = NarrationRun(
        project_id=project_id,
        script_id=script.id,
        script_version=script.version,
        voice_profile_id=profile.id,
        voice_profile_version=profile.version,
        idempotency_key="fixture-narration",
        input_hash="f" * 64,
        status="narration_complete",
        pipeline_version="narration/1.0.0",
        selected=True,
        preview_asset_id=preview_asset.id,
        total_duration_seconds=sum(len(text.split()) * seconds_per_word for text in texts),
        parameters={},
    )
    session.add(run)
    session.flush()
    segments: list[NarrationSegment] = []
    for index, (text, script_segment) in enumerate(zip(texts, script_segments, strict=True)):
        audio = assets.store(
            content=f"fixture-audio-{index}".encode(),
            kind="audio",
            media_type="audio/wav",
            project_id=project_id,
        )
        timings = word_timings(text, seconds_per_word=seconds_per_word)
        segment = NarrationSegment(
            narration_run_id=run.id,
            script_segment_id=script_segment.id,
            sequence=index,
            text_hash=f"{index:064d}",
            generation_identity=f"{index + 100:064d}",
            status="complete",
            normalized_asset_id=audio.id,
            duration_seconds=round(len(text.split()) * seconds_per_word, 6),
            alignment={"coverage": 1.0, "timings": timings},
            quality_report={"valid": True},
            word_timings=timings,
        )
        session.add(segment)
        session.flush()
        segments.append(segment)
    return run, segments


def reopen_fixture(
    fixture: StoryboardFixture,
    source: Path,
    target: Path,
    *,
    database_name: str = "storyboard.db",
) -> tuple[Session, FilesystemBlobStore]:
    """Copy a committed fixture so the same inputs can be re-run from scratch.

    Determinism is a property of the pipeline, not of the fixture's random UUIDs,
    so a byte-identical database is the only fair way to compare two runs.
    """
    fixture.session.commit()
    fixture.session.close()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(source / database_name, target / database_name)
    shutil.copytree(source / "blobs", target / "blobs", dirs_exist_ok=True)
    engine = create_engine(f"sqlite:///{target / database_name}")
    session = Session(engine, expire_on_commit=False)
    return session, FilesystemBlobStore(target / "blobs", b"storyboard-secret")
