"""A complete, deterministic T22 fixture: ten passing shots and a real render.

The fixture reuses the T20 project graph - real shot media, approved T19
references, completed narration with word timings - runs T20 video QA against
the deterministic fake agent so every shot carries a passing canonical result,
then assembles a genuine T17 delivery with FFmpeg: a normalized picture track,
a real narration mix and real caption assets, described by a real
:class:`RenderManifest`.

Every defect fixture is produced by changing exactly one thing about that
delivery, so a blocking assertion always isolates the defect it names.

Nothing here makes a paid provider call.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from services.qa.commands import VisualQACommandOptions, run_visual_qa
from services.qa.final_rubric import DEFAULT_CONFIGURATION
from services.renderer.captions import (
    build_caption_track,
    caption_identity,
    serialize_srt,
    serialize_webvtt,
)
from services.renderer.manifest import bound_manifest_identity, render_identity
from tests.visual_qa_fixtures import build_visual_qa_project
from vidgen.contracts.final_editorial import FinalQAConfiguration
from vidgen.contracts.render import (
    CaptionTrack,
    CaptionWord,
    RenderAudioEntry,
    RenderInputReference,
    RenderManifest,
    RenderShotEntry,
)
from vidgen.contracts.visual_qa import VisualQATargetType
from vidgen.db import Base
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.models import Asset, RenderJob
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore

#: A small delivery profile keeps the fixture honest and the suite fast: every
#: check runs against a real encode, just not a 1080p one.
DELIVERY_WIDTH = 640
DELIVERY_HEIGHT = 360
DELIVERY_FPS = 24
SHOT_SECONDS = 3.0
SHOT_DURATION_US = 3_000_000

#: The configuration the fixture's delivery is graded against. It differs from
#: the production default only in the delivery profile and the tolerances a
#: single-pass loudness normalization needs.
FIXTURE_CONFIGURATION: FinalQAConfiguration = DEFAULT_CONFIGURATION.model_copy(
    update={
        "expected_width": DELIVERY_WIDTH,
        "expected_height": DELIVERY_HEIGHT,
        "expected_frame_rate": DELIVERY_FPS,
        "loudness_tolerance_lu": 3.0,
        "min_bytes_per_second": 1_000,
        "max_bytes_per_second": 3_500_000,
    }
)


@dataclass(slots=True)
class FinalQAFixture:
    """One project with a current, valid T17 render ready for T22."""

    project_id: UUID
    storyboard_run_id: UUID
    render_job_id: UUID
    shot_ids: list[UUID]
    final_video_asset_id: UUID
    manifest_asset_id: UUID
    srt_asset_id: UUID
    webvtt_asset_id: UUID
    narration_asset_id: UUID
    render_identity: str
    timeline_duration_us: int
    manifest: RenderManifest
    caption_track: CaptionTrack
    words: list[CaptionWord] = field(default_factory=list)
    workspace: Path = Path()


def ffmpeg(arguments: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode()[-800:])


def narration_wav(path: Path, *, segments: int, seconds: float) -> Path:
    """One continuous narration bed: a tone per segment, with brief joins.

    Real audio matters here. Loudness, true peak, clipping, silence and
    narration coverage are all measured from the delivered mix, so a fixture
    that stored text bytes as ``audio/wav`` would prove nothing.
    """
    inputs: list[str] = []
    filters: list[str] = []
    for index in range(segments):
        frequency = 220 + index * 40
        inputs.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration={seconds - 0.1:.3f}",
            ]
        )
        inputs.extend(
            ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.1"]
        )
        filters.extend([f"[{index * 2}:a]", f"[{index * 2 + 1}:a]"])
    graph = "".join(filters) + f"concat=n={segments * 2}:v=0:a=1[out]"
    ffmpeg(
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def assemble_render(
    output: Path,
    *,
    shots: list[Path],
    narration: Path,
    subtitles: Path,
    width: int = DELIVERY_WIDTH,
    height: int = DELIVERY_HEIGHT,
    fps: int = DELIVERY_FPS,
    audio_trim_seconds: float | None = None,
) -> Path:
    """Assemble the delivery exactly as T17 does: normalize, concat, mix, mux.

    ``audio_trim_seconds`` shortens the audio track only, which is how the
    audio/video drift fixture is produced without touching the picture.
    """
    inputs: list[str] = []
    filters: list[str] = []
    for index, shot in enumerate(shots):
        inputs.extend(["-i", str(shot)])
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{index}]"
        )
    concat = "".join(f"[v{index}]" for index in range(len(shots)))
    filters.append(f"{concat}concat=n={len(shots)}:v=1:a=0[vout]")
    inputs.extend(["-i", str(narration)])
    audio_filter = "loudnorm=I=-14:TP=-1.0:LRA=11"
    if audio_trim_seconds is not None:
        audio_filter = f"atrim=0:{audio_trim_seconds:.3f},asetpts=N/SR/TB,{audio_filter}"
    filters.append(f"[{len(shots)}:a]{audio_filter}[aout]")
    ffmpeg(
        [
            *inputs,
            "-i",
            str(subtitles),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-map",
            f"{len(shots) + 1}:s",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:s",
            "mov_text",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def build_final_qa_project(
    session: Session,
    blob_root: Path,
    workspace: Path,
    *,
    shot_count: int = 10,
    owner_subject: str = "local-user",
) -> FinalQAFixture:
    """Build one project with ten passing shots and a current T17 render."""
    workspace.mkdir(parents=True, exist_ok=True)
    store = FilesystemBlobStore(blob_root, b"test-secret")
    assets = AssetService(session, store)
    graph = build_visual_qa_project(
        session, blob_root, workspace / "t20", shot_count=shot_count, owner_subject=owner_subject
    )
    _run_passing_video_qa(session, store, graph.project_id)

    shots = list(
        session.scalars(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == graph.storyboard_run_id)
            .order_by(StoryboardShotRecord.global_sequence)
        )
    )[:shot_count]
    timeline_us = shots[-1].global_end_us

    words = _caption_words(session, graph.storyboard_run_id, shots)
    track, validation = build_caption_track(
        track_id=uuid4(), words=words, duration_us=timeline_us
    )
    assert validation.valid, validation.diagnostics
    srt = assets.store(
        content=serialize_srt(track).encode(),
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=graph.project_id,
        idempotency_key=f"t17-srt:{graph.project_id}",
    )
    webvtt = assets.store(
        content=serialize_webvtt(track).encode(),
        kind="subtitle",
        media_type="text/vtt",
        project_id=graph.project_id,
        idempotency_key=f"t17-vtt:{graph.project_id}",
    )

    narration_path = narration_wav(
        workspace / "narration.wav", segments=len(shots), seconds=SHOT_SECONDS
    )
    narration_asset = assets.store(
        content=narration_path.read_bytes(),
        kind="audio",
        media_type="audio/wav",
        project_id=graph.project_id,
        idempotency_key=f"t12-mix:{graph.project_id}",
    )
    narration_run = session.scalars(
        select(NarrationRun).where(
            NarrationRun.project_id == graph.project_id, NarrationRun.selected.is_(True)
        )
    ).one()
    narration_run.preview_asset_id = narration_asset.id

    shot_paths = _materialize_shots(session, store, workspace, shots)
    subtitle_path = workspace / "captions.srt"
    subtitle_path.write_text(serialize_srt(track), encoding="utf-8")
    final_path = assemble_render(
        workspace / "final.mp4",
        shots=shot_paths,
        narration=narration_path,
        subtitles=subtitle_path,
    )
    final_asset = assets.store(
        content=final_path.read_bytes(),
        kind="render",
        media_type="video/mp4",
        project_id=graph.project_id,
        idempotency_key=f"t17-final:{graph.project_id}",
    )

    manifest = _manifest(
        session,
        graph.project_id,
        graph.storyboard_run_id,
        shots=shots,
        track=track,
        words=words,
        narration_asset=narration_asset,
        narration_run_id=narration_run.id,
        caption_assets=[srt, webvtt],
        timeline_us=timeline_us,
    )
    manifest_asset = assets.store(
        content=json.dumps(manifest.model_dump(mode="json"), sort_keys=True).encode(),
        kind="json",
        media_type="application/json",
        project_id=graph.project_id,
        idempotency_key=f"t17-manifest:{graph.project_id}",
    )
    job = _update_render_job(
        session,
        project_id=graph.project_id,
        manifest=manifest,
        manifest_asset_id=manifest_asset.id,
        final_asset_id=final_asset.id,
        srt_asset_id=srt.id,
        webvtt_asset_id=webvtt.id,
        track=track,
        timeline_us=timeline_us,
    )
    session.commit()
    return FinalQAFixture(
        project_id=graph.project_id,
        storyboard_run_id=graph.storyboard_run_id,
        render_job_id=job.id,
        shot_ids=[shot.id for shot in shots],
        final_video_asset_id=final_asset.id,
        manifest_asset_id=manifest_asset.id,
        srt_asset_id=srt.id,
        webvtt_asset_id=webvtt.id,
        narration_asset_id=narration_asset.id,
        render_identity=manifest.render_identity,
        timeline_duration_us=timeline_us,
        manifest=manifest,
        caption_track=track,
        words=list(words),
        workspace=workspace,
    )


def replace_final_render(
    session: Session,
    blob_root: Path,
    fixture: FinalQAFixture,
    replacement: Path,
) -> UUID:
    """Swap the delivered media for a defective one, keeping the same manifest.

    The manifest still describes the intended delivery, which is exactly the
    situation a corrupted or mis-encoded render produces: T22 must catch it by
    measuring the file, not by reading the manifest.
    """
    store = FilesystemBlobStore(blob_root, b"test-secret")
    assets = AssetService(session, store)
    stored = assets.store(
        content=replacement.read_bytes(),
        kind="render",
        media_type="video/mp4",
        project_id=fixture.project_id,
        idempotency_key=f"t17-final-replacement:{replacement.name}:{fixture.project_id}",
    )
    job = session.get(RenderJob, fixture.render_job_id)
    assert job is not None
    job.final_video_asset_id = stored.id
    job.output_asset_id = stored.id
    session.commit()
    fixture.final_video_asset_id = stored.id
    return stored.id


# --- internals ---------------------------------------------------------------
def _run_passing_video_qa(session: Session, store: FilesystemBlobStore, project_id: UUID) -> None:
    """Give every shot a passing canonical T20 video-QA result."""
    outcome = asyncio.run(
        run_visual_qa(
            session,
            store,
            project_id=project_id,
            options=VisualQACommandOptions(
                provider="fake",
                targets=(VisualQATargetType.VIDEO,),
                idempotency_key=f"t20-video:{project_id}",
            ),
        )
    )
    assert not outcome.failures, outcome.failures
    session.commit()


def _materialize_shots(
    session: Session,
    store: FilesystemBlobStore,
    workspace: Path,
    shots: list[StoryboardShotRecord],
) -> list[Path]:
    paths: list[Path] = []
    for index, shot in enumerate(shots):
        video = session.scalars(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).one()
        asset = session.get(Asset, video.canonical_asset_id)
        assert asset is not None
        target = workspace / f"source-shot-{index}.mp4"
        store.copy_to(asset.storage_key, target)
        paths.append(target)
    return paths


def _caption_words(
    session: Session, storyboard_run_id: UUID, shots: list[StoryboardShotRecord]
) -> list[CaptionWord]:
    """Project approved T12 word timings onto the global timeline, in order."""
    segments = {
        segment.id: segment
        for segment in session.scalars(
            select(NarrationSegment).where(
                NarrationSegment.narration_run_id.in_(
                    select(StoryboardRun.narration_run_id).where(
                        StoryboardRun.id == storyboard_run_id
                    )
                )
            )
        )
    }
    words: list[CaptionWord] = []
    for shot in shots:
        segment = segments.get(shot.narration_segment_id)
        timings = list(segment.word_timings or []) if segment is not None else []
        for timing in timings:
            start = shot.global_start_us + round(float(timing["start_seconds"]) * 1_000_000)
            end = shot.global_start_us + round(float(timing["end_seconds"]) * 1_000_000)
            if end <= start or end > shot.global_end_us:
                continue
            words.append(
                CaptionWord(
                    sequence=len(words),
                    text=str(timing["word"]),
                    start_us=start,
                    end_us=end,
                )
            )
    return words


def _manifest(
    session: Session,
    project_id: UUID,
    storyboard_run_id: UUID,
    *,
    shots: list[StoryboardShotRecord],
    track: CaptionTrack,
    words: list[CaptionWord],
    narration_asset: object,
    narration_run_id: UUID,
    caption_assets: list[object],
    timeline_us: int,
) -> RenderManifest:
    script = session.scalars(
        select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
    ).one()
    entries: list[RenderShotEntry] = []
    for index, shot in enumerate(shots):
        video = session.scalars(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).one()
        duration = shot.global_end_us - shot.global_start_us
        entries.append(
            RenderShotEntry(
                shot_id=shot.id,
                sequence=index,
                shot_workflow_identity=f"{index + 1:064x}",
                animation_run_id=video.animation_item_id,
                video=RenderInputReference(
                    asset_id=video.canonical_asset_id,
                    sha256=video.sha256,
                    media_type="video/mp4",
                    role="locked_t15_clip",
                ),
                source_width=video.width or 320,
                source_height=video.height or 180,
                source_frame_rate=f"{DELIVERY_FPS}/1",
                source_codec="h264",
                measured_source_duration_us=round((video.canonical_duration or 3.0) * 1_000_000),
                global_start_us=shot.global_start_us,
                global_end_us=shot.global_end_us,
                exact_usable_duration_us=duration,
                trim_start_us=0,
                trim_end_us=duration,
            )
        )
    narration_reference = RenderInputReference(
        asset_id=narration_asset.id,  # type: ignore[attr-defined]
        sha256=narration_asset.sha256,  # type: ignore[attr-defined]
        media_type="audio/wav",
        role="narration_preview",
    )
    manifest = RenderManifest(
        manifest_id=uuid4(),
        render_identity="0" * 64,
        project_id=project_id,
        approved_script_id=script.id,
        approved_script_version=script.version,
        approved_script_hash=f"{1:064x}",
        narration_run_id=narration_run_id,
        narration_assets=[narration_reference],
        narration_word_timing_hash=render_identity(
            [word.model_dump(mode="json") for word in words]
        ),
        narration_duration_us=timeline_us,
        storyboard_run_id=storyboard_run_id,
        storyboard_hash=f"{2:064x}",
        timing_manifest_id=uuid4(),
        timing_manifest_hash=f"{3:064x}",
        t16_result_id="t22-fixture-locked",
        shots=entries,
        caption_track_id=track.caption_track_id,
        caption_identity=caption_identity(track),
        caption_assets=[
            RenderInputReference(
                asset_id=item.id,  # type: ignore[attr-defined]
                sha256=item.sha256,  # type: ignore[attr-defined]
                media_type=item.media_type,  # type: ignore[attr-defined]
                role=role,
            )
            for item, role in zip(caption_assets, ("caption_srt", "caption_webvtt"), strict=True)
        ],
        audio_entries=[
            RenderAudioEntry(
                role="narration",
                asset=RenderInputReference(
                    asset_id=narration_asset.id,  # type: ignore[attr-defined]
                    sha256=narration_asset.sha256,  # type: ignore[attr-defined]
                    media_type="audio/wav",
                    role="narration",
                ),
                duration_us=timeline_us,
            )
        ],
        video_profile={  # type: ignore[arg-type]
            "width": 1920,
            "height": 1080,
            "frame_rate": DELIVERY_FPS,
        },
        input_hash=f"{4:064x}",
        idempotency_key="t22-fixture",
        created_at=datetime.now(UTC),
        provenance={"fixture": "t22/1"},
    )
    return manifest.model_copy(update={"render_identity": bound_manifest_identity(manifest)})


def _update_render_job(
    session: Session,
    *,
    project_id: UUID,
    manifest: RenderManifest,
    manifest_asset_id: UUID,
    final_asset_id: UUID,
    srt_asset_id: UUID,
    webvtt_asset_id: UUID,
    track: CaptionTrack,
    timeline_us: int,
) -> RenderJob:
    job = session.scalars(
        select(RenderJob).where(
            RenderJob.project_id == project_id, RenderJob.selected.is_(True)
        )
    ).one()
    job.status = "render_complete"
    job.manifest_asset_id = manifest_asset_id
    job.output_asset_id = final_asset_id
    job.final_video_asset_id = final_asset_id
    job.srt_asset_id = srt_asset_id
    job.webvtt_asset_id = webvtt_asset_id
    job.render_identity = manifest.render_identity
    job.expected_duration_us = timeline_us
    job.measured_duration_us = timeline_us
    final_asset = session.get(Asset, final_asset_id)
    job.output_sha256 = final_asset.sha256 if final_asset is not None else None
    job.renderer_version = "t17/1"
    job.completed_at = datetime.now(UTC)
    record = session.scalars(
        select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == job.id)
    ).one_or_none()
    if record is not None:
        record.caption_identity = caption_identity(track)
        record.language = track.language
        record.cue_count = len(track.cues)
        record.start_us = 0
        record.end_us = timeline_us
        record.srt_asset_id = srt_asset_id
        record.webvtt_asset_id = webvtt_asset_id
    session.flush()
    return job


def require_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --- the session-wide template -----------------------------------------------
#: Building one project costs ten encoded shots, a full T20 video-QA pass over
#: them and a real delivery assembly - tens of seconds of FFmpeg. Every test
#: wants the same project, so it is built once per test session and copied.
#: The copy is what each test mutates; the template is never opened again.
@dataclass(frozen=True, slots=True)
class _Template:
    root: Path
    fixture: FinalQAFixture


_TEMPLATES: dict[tuple[int, str], _Template] = {}


def _template(shot_count: int, owner_subject: str) -> _Template:
    key = (shot_count, owner_subject)
    cached = _TEMPLATES.get(key)
    if cached is not None:
        return cached
    root = Path(tempfile.mkdtemp(prefix="vidgen-t22-template-"))
    atexit.register(shutil.rmtree, root, True)
    engine = create_engine(f"sqlite+pysqlite:///{root / 'template.db'}")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            fixture = build_final_qa_project(
                session,
                root / "blobs",
                root / "work",
                shot_count=shot_count,
                owner_subject=owner_subject,
            )
    finally:
        # The database file is copied byte for byte, so nothing may still hold
        # a connection to it.
        engine.dispose()
    # The T20 sub-workspace holds the PNG frames the shot encodes were built
    # from. Nothing reads them again, and they dwarf everything else on disk.
    shutil.rmtree(root / "work" / "t20", ignore_errors=True)
    template = _Template(root=root, fixture=fixture)
    _TEMPLATES[key] = template
    return template


def materialize_final_qa_project(
    *,
    database_path: Path,
    blob_root: Path,
    workspace: Path,
    shot_count: int = 10,
    owner_subject: str = "local-user",
) -> FinalQAFixture:
    """Copy the prebuilt project into one test's own database and directories.

    The returned fixture is a private copy: a test may swap the delivered
    render, add assets or advance the project without touching another test.
    Call this before opening an engine on ``database_path`` - the file is
    replaced wholesale.
    """
    template = _template(shot_count, owner_subject)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template.root / "template.db", database_path)
    shutil.copytree(template.root / "blobs", blob_root, dirs_exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in sorted((template.root / "work").iterdir()):
        if entry.is_file():
            shutil.copyfile(entry, workspace / entry.name)
    fixture = deepcopy(template.fixture)
    fixture.workspace = workspace
    return fixture
