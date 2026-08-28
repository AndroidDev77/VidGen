"""Deterministic T20 fixtures: a real project graph with controlled defects.

Media is synthesised during test setup with FFmpeg and Pillow - nothing large or
copyrighted is committed. Each controlled defect is a small, exactly described
deviation (black frames, a frozen clip, readable text, a duration mismatch, a
corrupt file), so a deterministic check has something real to measure.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from tests.review_fixtures import SHOT_DURATION_US, build_project_graph, digest
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_sets,
    character_state_snapshots,
    location_identity_versions,
    location_reference_sets,
    location_state_snapshots,
    shot_reference_bindings,
)
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord
from vidgen.db.image_generation_models import GeneratedKeyframeImage
from vidgen.db.models import Character, Location
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.storyboard_models import StoryboardShotRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore

WIDTH = 320
HEIGHT = 180
FRAME_RATE = 24
SHOT_SECONDS = SHOT_DURATION_US / 1_000_000


@dataclass(slots=True)
class VisualQAFixture:
    """One project ready for T20, with the IDs the assertions need."""

    project_id: UUID
    storyboard_run_id: UUID
    shot_ids: list[UUID]
    stable_shot_ids: list[UUID]
    character_id: UUID
    location_id: UUID
    character_identity_version_id: UUID
    location_identity_version_id: UUID
    reference_asset_ids: list[UUID] = field(default_factory=list)


# --- synthetic media ---------------------------------------------------------
def _run(arguments: list[str]) -> None:
    completed = subprocess.run(arguments, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode()[-500:])


def make_image(path: Path, *, text: str | None = None, tint: int = 120) -> Path:
    """A deterministic still frame, optionally carrying readable text."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (40, tint, 150))
    draw = ImageDraw.Draw(image)
    draw.ellipse([90, 40, 230, 150], fill=(220, 180, 150))
    if text:
        for index in range(3):
            draw.text((12, 20 + index * 12), text, fill=(255, 255, 255))
    image.save(path, format="PNG")
    return path


def _render_frames(path: Path, *, seconds: float) -> Path:
    """A clean clip built from rendered frames: real motion, no rendered text.

    Synthetic lavfi test patterns draw counters and captions, which the
    unintended-text check correctly flags. A clean fixture must not contain any.
    """
    frames = max(1, round(seconds * FRAME_RATE))
    directory = path.parent / f"{path.stem}-frames"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(frames):
        image = Image.new("RGB", (WIDTH, HEIGHT), (40, 120, 150))
        draw = ImageDraw.Draw(image)
        offset = round(index * (WIDTH - 140) / max(1, frames - 1))
        draw.ellipse([20 + offset, 40, 140 + offset, 150], fill=(220, 180, 150))
        draw.rectangle([0, HEIGHT - 24, WIDTH, HEIGHT], fill=(30, 90, 120))
        image.save(directory / f"frame-{index:05d}.png", format="PNG")
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(FRAME_RATE),
            "-i",
            str(directory / "frame-%05d.png"),
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-fflags",
            "+bitexact",
            str(path),
        ]
    )
    return path


def make_video(
    path: Path,
    *,
    seconds: float = SHOT_SECONDS,
    kind: str = "clean",
) -> Path:
    """Render one controlled clip. ``kind`` names the deliberate defect."""
    if kind == "corrupt":
        path.write_bytes(b"not-a-video" * 64)
        return path
    if kind == "black":
        colour = f"color=c=black:s={WIDTH}x{HEIGHT}:d={seconds}:r={FRAME_RATE}"
        source = ["-f", "lavfi", "-i", colour]
        filters = "null"
    elif kind == "freeze":
        colour = f"color=c=0x2878A0:s={WIDTH}x{HEIGHT}:d={seconds}:r={FRAME_RATE}"
        source = ["-f", "lavfi", "-i", colour]
        filters = "null"
    elif kind == "flicker":
        source = [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s={WIDTH}x{HEIGHT}:d={seconds}:r={FRAME_RATE}",
        ]
        filters = "eq=brightness='if(eq(mod(n,2),0),0.5,-0.5)':eval=frame"
    else:
        return _render_frames(path, seconds=seconds)
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *source,
            "-vf",
            filters,
            "-frames:v",
            str(max(1, round(seconds * FRAME_RATE))),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-fflags",
            "+bitexact",
            str(path),
        ]
    )
    return path


# --- storyboard shot contract ------------------------------------------------
def shot_contract(
    *,
    shot_id: UUID,
    storyboard_run_id: UUID,
    segment_id: UUID,
    script_segment_id: UUID,
    narration_segment_id: UUID,
    sequence: int,
    character_id: UUID,
    location_id: UUID,
    importance: float,
) -> dict[str, object]:
    """A complete, valid ``StoryboardShot`` payload for one shot."""
    generation_us = SHOT_DURATION_US + 1_000_000
    return {
        "schema_version": "1.0",
        "shot_id": str(shot_id),
        "storyboard_run_id": str(storyboard_run_id),
        "segment_id": str(segment_id),
        "global_sequence": sequence,
        "segment_sequence": 0,
        "script_segment_id": str(script_segment_id),
        "narration_segment_id": str(narration_segment_id),
        "start_us": 0,
        "end_us": SHOT_DURATION_US,
        "global_start_us": sequence * SHOT_DURATION_US,
        "global_end_us": (sequence + 1) * SHOT_DURATION_US,
        "usable_duration_us": SHOT_DURATION_US,
        "requested_generation_duration_us": generation_us,
        "trim_start_us": 0,
        "trim_end_us": generation_us - SHOT_DURATION_US,
        "transition_handle_us": 0,
        "word_start_index": sequence * 6,
        "word_end_index": sequence * 6 + 6,
        "clause_label": f"beat-{sequence + 1}",
        "visual_objective": f"Show beat {sequence + 1} in a wide comic frame.",
        "requires_last_frame": False,
        "camera": {
            "framing": "medium",
            "angle": "eye_level",
            "movement": "static",
            "movement_intensity": "none",
        },
        "action": {
            "subject_action": f"The lead reacts to beat {sequence + 1}",
            "beat_intent": "react",
            "prop_references": ["mug"],
        },
        "character_reference_ids": [str(character_id)],
        "location_reference_id": str(location_id),
        "prop_references": ["mug"],
        "transition_in": {"kind": "cut"},
        "transition_out": {"kind": "cut"},
        "incoming_continuity": {
            "present_character_ids": [str(character_id)],
            "character_appearance_states": [
                {
                    "character_id": str(character_id),
                    "appearance_state_id": "default",
                    "wardrobe_state": "green jacket",
                }
            ],
            "location_id": str(location_id),
            "time_of_day": "midday",
            "props": [{"prop_id": "mug", "owner_character_id": str(character_id)}],
            "subject_positions": [
                {
                    "character_id": str(character_id),
                    "screen_position": "center",
                    "facing": "left_to_right",
                }
            ],
            "screen_direction": "left_to_right",
            "emotional_state": "amused",
        },
        "expected_outgoing_continuity": {
            "present_character_ids": [str(character_id)],
            "location_id": str(location_id),
            "time_of_day": "midday",
            "screen_direction": "left_to_right",
            "emotional_state": "amused",
        },
        "capability_profile_id": "fake-video/1",
        "capability_hash": digest(f"capability-{sequence}"),
        "provenance": {"importance": importance},
    }


# --- fixture builder ---------------------------------------------------------
def build_visual_qa_project(
    session: Session,
    blob_root: Path,
    workspace: Path,
    *,
    owner_subject: str = "local-user",
    name: str = "Season 3 Episode 4",
    shot_count: int = 3,
    defects: dict[int, str] | None = None,
) -> VisualQAFixture:
    """Build one project whose first ``shot_count`` shots carry real media."""
    workspace.mkdir(parents=True, exist_ok=True)
    store = FilesystemBlobStore(blob_root, b"test-secret")
    assets = AssetService(session, store)
    graph = build_project_graph(
        session, owner_subject=owner_subject, name=name, blob_root=blob_root, with_render=True
    )
    analysis = session.query(EpisodeAnalysisRecord).filter_by(project_id=graph.project_id).one()
    character = Character(project_id=graph.project_id, canonical_name="Maya", definition={})
    location = Location(project_id=graph.project_id, canonical_name="Kitchen", definition={})
    session.add_all([character, location])
    session.flush()

    reference_ids: list[UUID] = []
    character_reference = assets.store(
        content=make_image(workspace / "character-reference.png").read_bytes(),
        kind="character_reference",
        media_type="image/png",
        project_id=graph.project_id,
        idempotency_key=f"t19-character-reference:{graph.project_id}",
    )
    location_reference = assets.store(
        content=make_image(workspace / "location-reference.png", tint=125).read_bytes(),
        kind="location_reference",
        media_type="image/png",
        project_id=graph.project_id,
        idempotency_key=f"t19-location-reference:{graph.project_id}",
    )
    reference_ids.extend([character_reference.id, location_reference.id])

    character_version_id = _identity_version(
        session,
        character_identity_versions,
        project_id=graph.project_id,
        analysis_id=analysis.id,
        entity_column="character_id",
        entity_id=character.id,
        bible={
            "schema_version": "1.0",
            "character_id": str(character.id),
            "display_name": "Maya",
            "aliases": [],
            "stable_traits": {"face": "round", "hair": "black bob", "skin_tone": "warm tan"},
            "evidence": [],
            "confidence": 0.94,
            "ambiguities": [],
        },
    )
    location_version_id = _identity_version(
        session,
        location_identity_versions,
        project_id=graph.project_id,
        analysis_id=analysis.id,
        entity_column="location_id",
        entity_id=location.id,
        bible={
            "schema_version": "1.0",
            "location_id": str(location.id),
            "display_name": "Kitchen",
            "location_type": "interior",
            "stable_traits": {"layout": "galley", "landmarks": "yellow fridge"},
            "evidence": [],
            "confidence": 0.93,
            "ambiguities": [],
        },
    )
    _reference_set(
        session,
        character_reference_sets,
        project_id=graph.project_id,
        identity_version_id=character_version_id,
        asset_id=character_reference.id,
    )
    _reference_set(
        session,
        location_reference_sets,
        project_id=graph.project_id,
        identity_version_id=location_version_id,
        asset_id=location_reference.id,
    )

    _complete_narration(session, graph.project_id, assets)
    shots = (
        session.query(StoryboardShotRecord)
        .filter_by(storyboard_run_id=graph.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
        .all()
    )
    fixture = VisualQAFixture(
        project_id=graph.project_id,
        storyboard_run_id=graph.storyboard_run_id,
        shot_ids=[],
        stable_shot_ids=[],
        character_id=character.id,
        location_id=location.id,
        character_identity_version_id=character_version_id,
        location_identity_version_id=location_version_id,
        reference_asset_ids=reference_ids,
    )
    for index, shot in enumerate(shots[:shot_count]):
        # Shot 1 is the hero shot; the rest are normal.
        importance = 0.9 if index == 1 else 0.5
        shot.contract = shot_contract(
            shot_id=shot.stable_shot_id,
            storyboard_run_id=graph.storyboard_run_id,
            segment_id=shot.segment_checkpoint_id,
            script_segment_id=shot.script_segment_id,
            narration_segment_id=shot.narration_segment_id,
            sequence=shot.global_sequence,
            character_id=character.id,
            location_id=location.id,
            importance=importance,
        )
        kind = (defects or {}).get(index, "clean")
        keyframe_path = make_image(
            workspace / f"keyframe-{index}.png",
            text="SUBSCRIBE NOW for more videos" if kind == "text" else None,
        )
        keyframe_asset = assets.store(
            content=keyframe_path.read_bytes(),
            kind="keyframe",
            media_type="image/png",
            project_id=graph.project_id,
            idempotency_key=f"t14-keyframe:{shot.id}",
        )
        keyframe = (
            session.query(GeneratedKeyframeImage)
            .filter_by(shot_id=shot.id, keyframe_role="FIRST_FRAME")
            .one()
        )
        keyframe.asset_id = keyframe_asset.id
        keyframe.sha256 = keyframe_asset.sha256
        keyframe.width, keyframe.height = WIDTH, HEIGHT
        keyframe.byte_size = keyframe_asset.byte_size

        seconds = SHOT_SECONDS + (0.5 if kind == "duration" else 0.0)
        video_path = make_video(
            workspace / f"shot-{index}.mp4",
            seconds=seconds,
            kind=kind if kind in {"black", "freeze", "flicker", "corrupt"} else "clean",
        )
        video_asset = assets.store(
            content=video_path.read_bytes(),
            kind="canonical_shot_video",
            media_type="video/mp4",
            project_id=graph.project_id,
            idempotency_key=f"t15-canonical:{shot.id}",
        )
        video = session.query(AnimationGeneratedVideo).filter_by(shot_id=shot.id).one()
        video.canonical_asset_id = video_asset.id
        video.sha256 = video_asset.sha256
        video.canonical_duration = seconds
        video.width, video.height = WIDTH, HEIGHT

        character_snapshot = _state_snapshot(
            session,
            character_state_snapshots,
            identity_version_id=character_version_id,
            shot_id=shot.id,
            state={
                "schema_version": "1.0",
                "interval": {"start_sequence": 0},
                "wardrobe": ["green jacket"],
                "hairstyle": "black bob",
                "injuries": [],
                "carried_props": ["mug"],
                "confidence": 0.9,
            },
        )
        location_snapshot = _state_snapshot(
            session,
            location_state_snapshots,
            identity_version_id=location_version_id,
            shot_id=shot.id,
            state={
                "schema_version": "1.0",
                "interval": {"start_sequence": 0},
                "time_of_day": "midday",
                "lighting": "warm overhead",
                "damage": [],
                "confidence": 0.9,
            },
        )
        bundle = {
            "schema_version": "1.0",
            "id": str(uuid4()),
            "project_id": str(graph.project_id),
            "storyboard_run_id": str(graph.storyboard_run_id),
            "shot_id": str(shot.id),
            "shot_sequence": shot.global_sequence,
            "character_identity_version_ids": [str(character_version_id)],
            "character_state_snapshot_ids": [str(character_snapshot)],
            "location_identity_version_id": str(location_version_id),
            "location_state_snapshot_id": str(location_snapshot),
            "references": [
                {
                    "asset_id": str(character_reference.id),
                    "sha256": character_reference.sha256,
                    "role": "character_identity",
                    "entity_id": str(character.id),
                    "required": True,
                    "priority": 0,
                },
                {
                    "asset_id": str(location_reference.id),
                    "sha256": location_reference.sha256,
                    "role": "location_identity",
                    "entity_id": str(location.id),
                    "required": True,
                    "priority": 1,
                },
            ],
            "required_props": ["mug"],
            "provider_reference_limit": 4,
            "resolver_version": "t19/1",
        }
        session.execute(
            shot_reference_bindings.insert().values(
                id=uuid4(),
                project_id=graph.project_id,
                storyboard_id=graph.storyboard_run_id,
                storyboard_shot_id=shot.id,
                bundle=bundle,
                bundle_hash=digest(f"bundle:{shot.id}"),
                status="approved",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        fixture.shot_ids.append(shot.id)
        fixture.stable_shot_ids.append(shot.stable_shot_id)
    session.commit()
    return fixture


def _complete_narration(session: Session, project_id: UUID, assets: AssetService) -> None:
    """Give the T18 graph the measured narration T17 render selection requires.

    The review fixture stops at durations; render eligibility also needs
    normalized audio and word timings, which is what makes the T20
    render-blocking assertion meaningful.
    """
    run = session.query(NarrationRun).filter_by(project_id=project_id, selected=True).one()
    segments = (
        session.query(NarrationSegment)
        .filter_by(narration_run_id=run.id)
        .order_by(NarrationSegment.sequence)
        .all()
    )
    for segment in segments:
        stored = assets.store(
            content=f"synthetic-narration:{segment.id}".encode(),
            kind="audio",
            media_type="audio/wav",
            project_id=project_id,
            idempotency_key=f"t12-normalized:{segment.id}",
        )
        segment.normalized_asset_id = stored.id
        segment.duration_seconds = SHOT_SECONDS
        segment.word_timings = [
            {
                "word_index": index,
                "word": f"word{index}",
                "start_seconds": index * 0.5,
                "end_seconds": (index + 1) * 0.5,
            }
            for index in range(6)
        ]
    run.total_duration_seconds = SHOT_SECONDS * len(segments)
    session.flush()


def _now() -> object:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _identity_version(
    session: Session,
    table: object,
    *,
    project_id: UUID,
    analysis_id: UUID,
    entity_column: str,
    entity_id: UUID,
    bible: dict[str, object],
) -> UUID:
    identifier = uuid4()
    session.execute(
        table.insert().values(  # type: ignore[attr-defined]
            id=identifier,
            project_id=project_id,
            **{entity_column: entity_id},
            episode_analysis_id=analysis_id,
            version=1,
            identity=bible,
            identity_hash=digest(f"identity:{identifier}"),
            status="approved",
            approved_by="local-user",
            approved_at=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return identifier


def _reference_set(
    session: Session,
    table: object,
    *,
    project_id: UUID,
    identity_version_id: UUID,
    asset_id: UUID,
) -> UUID:
    identifier = uuid4()
    session.execute(
        table.insert().values(  # type: ignore[attr-defined]
            id=identifier,
            project_id=project_id,
            identity_version_id=identity_version_id,
            reference_identity=digest(f"reference:{identifier}"),
            status="approved",
            ordered_asset_ids=[str(asset_id)],
            primary_asset_id=asset_id,
            validation_report={"valid": True},
            row_version=1,
            approved_by="local-user",
            approved_at=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return identifier


def _state_snapshot(
    session: Session,
    table: object,
    *,
    identity_version_id: UUID,
    shot_id: UUID,
    state: dict[str, object],
) -> UUID:
    identifier = uuid4()
    session.execute(
        table.insert().values(  # type: ignore[attr-defined]
            id=identifier,
            identity_version_id=identity_version_id,
            storyboard_shot_id=shot_id,
            chronology_interval={"start_sequence": 0},
            state=state,
            evidence_references=[],
            snapshot_hash=digest(f"snapshot:{identifier}"),
            resolver_version="t19/1",
            created_at=_now(),
        )
    )
    return identifier


def image_bytes(text: str | None = None) -> bytes:
    """An in-memory synthetic frame for unit tests that need image content."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (40, 120, 150))
    draw = ImageDraw.Draw(image)
    draw.ellipse([90, 40, 230, 150], fill=(220, 180, 150))
    if text:
        for index in range(3):
            draw.text((12, 20 + index * 12), text, fill=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
