from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from services.renderer.captions import (
    build_caption_track,
    serialize_ass,
    serialize_srt,
    serialize_webvtt,
)
from services.renderer.commands import build_command_plan
from services.renderer.manifest import bound_manifest_identity, render_identity
from services.renderer.pipeline import DeterministicRenderPipeline, FilesystemArtifactStore
from services.renderer.render import CommandExecutor
from services.renderer.verify import decode_complete, probe
from vidgen.contracts.render import (
    CaptionWord,
    RenderAudioEntry,
    RenderInputReference,
    RenderManifest,
    RenderShotEntry,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")


def run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=True,
        capture_output=True,
    )


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def test_deterministic_ten_shot_render_and_completed_identity_reuse(tmp_path: Path) -> None:
    media = tmp_path / "fixture"
    media.mkdir()
    source_by_id: dict[object, Path] = {}
    shots: list[RenderShotEntry] = []
    colors = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan", "white", "gray"]
    for sequence, color in enumerate(colors):
        source = media / f"shot-{sequence}.mp4"
        run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=24:d=0.25",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                source.as_posix(),
            ]
        )
        asset_id = uuid4()
        source_by_id[asset_id] = source
        shots.append(
            RenderShotEntry(
                shot_id=uuid4(),
                sequence=sequence,
                shot_workflow_identity=f"{sequence + 1:064x}",
                animation_run_id=uuid4(),
                video=RenderInputReference(
                    asset_id=asset_id,
                    sha256=digest(source),
                    media_type="video/mp4",
                    role="locked_t15_clip",
                ),
                source_width=320,
                source_height=180,
                source_frame_rate="24/1",
                source_codec="h264",
                measured_source_duration_us=250_000,
                global_start_us=sequence * 250_000,
                global_end_us=(sequence + 1) * 250_000,
                exact_usable_duration_us=250_000,
                trim_start_us=0,
                trim_end_us=250_000,
            )
        )

    narration = media / "narration.wav"
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.5",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            narration.as_posix(),
        ]
    )
    narration_id = uuid4()
    source_by_id[narration_id] = narration
    words = [
        CaptionWord(
            sequence=i,
            text=(f"shot {i + 1}" + ("." if i in {4, 9} else ",")),
            start_us=i * 250_000,
            end_us=(i + 1) * 250_000,
        )
        for i in range(10)
    ]
    caption_track, validation = build_caption_track(
        track_id=uuid4(), words=words, duration_us=2_500_000
    )
    assert validation.valid

    store = FilesystemArtifactStore(tmp_path / "assets")
    srt = store.store_bytes(
        content=serialize_srt(caption_track).encode(),
        media_type="application/x-subrip",
        kind="caption_srt",
        identity_key="fixture:srt",
    )
    webvtt = store.store_bytes(
        content=serialize_webvtt(caption_track).encode(),
        media_type="text/vtt",
        kind="caption_webvtt",
        identity_key="fixture:vtt",
    )
    material = {
        "project_id": str(uuid4()),
        "shots": [
            (str(s.shot_id), s.video.sha256, s.global_start_us, s.global_end_us) for s in shots
        ],
        "narration": digest(narration),
        "caption": validation.caption_identity,
        "profile": "t17/1",
    }
    identity = render_identity(material)
    project_id = uuid4()
    manifest = RenderManifest(
        manifest_id=uuid4(),
        render_identity=identity,
        project_id=project_id,
        approved_script_id=uuid4(),
        approved_script_version=1,
        approved_script_hash="a" * 64,
        narration_run_id=uuid4(),
        narration_assets=[
            RenderInputReference(
                asset_id=narration_id,
                sha256=digest(narration),
                media_type="audio/wav",
                role="narration_preview",
            )
        ],
        narration_word_timing_hash=render_identity(
            [word.model_dump(mode="json") for word in words]
        ),
        narration_duration_us=2_500_000,
        storyboard_run_id=uuid4(),
        storyboard_hash="b" * 64,
        timing_manifest_id=uuid4(),
        timing_manifest_hash="c" * 64,
        t16_result_id="fixture-t16-locked",
        shots=shots,
        caption_track_id=caption_track.caption_track_id,
        caption_identity=validation.caption_identity,
        caption_assets=[
            RenderInputReference(
                asset_id=srt.asset_id,
                sha256=srt.sha256,
                media_type=srt.media_type,
                role="caption_srt",
            ),
            RenderInputReference(
                asset_id=webvtt.asset_id,
                sha256=webvtt.sha256,
                media_type=webvtt.media_type,
                role="caption_webvtt",
            ),
        ],
        audio_entries=[
            RenderAudioEntry(
                role="narration",
                asset=RenderInputReference(
                    asset_id=narration_id,
                    sha256=digest(narration),
                    media_type="audio/wav",
                    role="narration",
                ),
                duration_us=2_500_000,
            )
        ],
        input_hash=render_identity(material),
        idempotency_key="ten-shot-fixture",
        created_at=datetime.now(UTC),
        provenance={"fixture": "ten-shot/1"},
    )
    manifest = manifest.model_copy(update={"render_identity": bound_manifest_identity(manifest)})

    def resolve(asset_id: object, destination: Path) -> None:
        with source_by_id[asset_id].open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)

    executor = CommandExecutor(timeout_seconds=180)
    pipeline = DeterministicRenderPipeline(
        store=store, work_root=tmp_path / "work", executor=executor
    )
    first = pipeline.run(manifest=manifest, caption_track=caption_track, resolve_asset=resolve)
    execution_count = executor.executions
    assert execution_count == 15
    metadata = probe(first.final_video.path)
    streams = metadata["streams"]
    assert [(item["codec_type"], item["codec_name"]) for item in streams] == [
        ("video", "h264"),
        ("audio", "aac"),
        ("subtitle", "mov_text"),
    ]
    assert streams[0]["width"] == 1920 and streams[0]["height"] == 1080
    assert streams[0]["pix_fmt"] == "yuv420p"
    assert streams[1]["sample_rate"] == "48000"
    decode_complete(first.final_video.path)
    report = json.loads(first.verification_report.path.read_text())
    assert report["full_decode_ok"] and report["subtitle_valid"]
    assert first.srt.path.exists() and first.webvtt.path.exists()

    second = pipeline.run(manifest=manifest, caption_track=caption_track, resolve_asset=resolve)
    assert second.reused
    assert second.final_video.asset_id == first.final_video.asset_id
    assert executor.executions == execution_count

    changed_cue = caption_track.cues[0].model_copy(update={"lines": ["different approved text"]})
    changed_track = caption_track.model_copy(
        update={"cues": [changed_cue, *caption_track.cues[1:]]}
    )
    with pytest.raises(ValueError, match="caption content identity"):
        pipeline.run(manifest=manifest, caption_track=changed_track, resolve_asset=resolve)
    assert executor.executions == execution_count

    ass = store.store_bytes(
        content=serialize_ass(caption_track).encode(),
        media_type="text/x-ssa",
        kind="caption_ass",
        identity_key="fixture:ass",
    )
    ass_reference = RenderInputReference(
        asset_id=ass.asset_id,
        sha256=ass.sha256,
        media_type=ass.media_type,
        role="caption_ass",
    )
    both_manifest = manifest.model_copy(
        update={
            "subtitle_mode": "both",
            "caption_assets": [*manifest.caption_assets, ass_reference],
        }
    )
    both_manifest = both_manifest.model_copy(
        update={"render_identity": bound_manifest_identity(both_manifest)}
    )
    both_root = tmp_path / "both"
    both_root.mkdir()
    assert first.picture_master is not None and first.normalized_audio is not None
    shutil.copyfile(first.picture_master.path, both_root / "picture.mp4")
    shutil.copyfile(first.normalized_audio.path, both_root / "master.wav")
    (both_root / "captions.srt").write_text(serialize_srt(caption_track), encoding="utf-8")
    (both_root / "captions.ass").write_text(serialize_ass(caption_track), encoding="utf-8")
    both_plan = build_command_plan(both_manifest, both_root)
    assert "-vf" in both_plan.final_arguments
    assert "-c:s" in both_plan.final_arguments
    assert any(argument.startswith("ass=filename=") for argument in both_plan.final_arguments)
    executor.run(both_plan.final_arguments, "acceptance:both-subtitle-modes")
    assert [
        stream["codec_name"]
        for stream in probe(both_root / "final.mp4")["streams"]
        if stream["codec_type"] == "subtitle"
    ] == ["mov_text"]

    burn_manifest = both_manifest.model_copy(update={"subtitle_mode": "burn_in"})
    burn_manifest = burn_manifest.model_copy(
        update={"render_identity": bound_manifest_identity(burn_manifest)}
    )
    burn_root = tmp_path / "burn"
    burn_root.mkdir()
    shutil.copyfile(first.picture_master.path, burn_root / "picture.mp4")
    shutil.copyfile(first.normalized_audio.path, burn_root / "master.wav")
    (burn_root / "captions.ass").write_text(serialize_ass(caption_track), encoding="utf-8")
    burn_plan = build_command_plan(burn_manifest, burn_root)
    assert "-vf" in burn_plan.final_arguments
    assert "-c:s" not in burn_plan.final_arguments
    executor.run(burn_plan.final_arguments, "acceptance:burn-in-only")
    assert not [
        stream
        for stream in probe(burn_root / "final.mp4")["streams"]
        if stream["codec_type"] == "subtitle"
    ]
