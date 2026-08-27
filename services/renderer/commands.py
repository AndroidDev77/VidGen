"""Subprocess argument-array plans derived solely from an immutable manifest."""

from __future__ import annotations

from pathlib import Path

from services.renderer.filters import normalization_filter, validate_transitions
from services.renderer.manifest import render_identity
from vidgen.contracts.render import RenderCommandPlan, RenderManifest


def build_command_plan(manifest: RenderManifest, root: Path) -> RenderCommandPlan:
    validate_transitions(manifest)
    normalized = []
    for shot in manifest.shots:
        normalized.append(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(root / f"{shot.video.sha256}.input"),
                "-an",
                "-vf",
                normalization_filter(manifest, shot.sequence),
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                str(root / f"shot-{shot.sequence:04}.mp4"),
            ]
        )
    concat = root / "concat.txt"
    picture = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        str(concat),
        "-c",
        "copy",
        str(root / "picture.mp4"),
    ]
    narration = next(item for item in manifest.audio_entries if item.role == "narration")
    premaster = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(root / f"{narration.asset.sha256}.input"),
        "-af",
        "aresample=48000,aformat=channel_layouts=stereo,alimiter=limit=0.84",
        "-c:a",
        "pcm_s24le",
        str(root / "premaster.wav"),
    ]
    pass1 = [
        "ffmpeg",
        "-nostdin",
        "-i",
        str(root / "premaster.wav"),
        "-af",
        f"loudnorm=I={manifest.audio_profile.integrated_lufs}:TP={manifest.audio_profile.true_peak_dbtp}:LRA={manifest.audio_profile.max_lra}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    pass2 = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(root / "premaster.wav"),
        "-af",
        f"loudnorm=I={manifest.audio_profile.integrated_lufs}:TP={manifest.audio_profile.true_peak_dbtp}:LRA={manifest.audio_profile.max_lra}",
        "-c:a",
        "pcm_s24le",
        str(root / "master.wav"),
    ]
    final = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(root / "picture.mp4"),
        "-i",
        str(root / "master.wav"),
        "-i",
        str(root / "captions.srt"),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:s:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        f"{manifest.audio_profile.bitrate_kbps}k",
        "-ar",
        "48000",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=eng",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(root / "final.mp4"),
    ]
    data = {
        "normalization_arguments": normalized,
        "picture_arguments": picture,
        "premaster_arguments": premaster,
        "loudness_pass1_arguments": pass1,
        "loudness_pass2_arguments": pass2,
        "final_arguments": final,
    }
    return RenderCommandPlan(
        render_identity=manifest.render_identity,
        normalization_arguments=normalized,
        picture_arguments=picture,
        premaster_arguments=premaster,
        loudness_pass1_arguments=pass1,
        loudness_pass2_arguments=pass2,
        final_arguments=final,
        command_plan_hash=render_identity(data),
    )
