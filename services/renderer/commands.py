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
                "-x264-params",
                "bframes=0:scenecut=0",
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
        "0",
        "-i",
        str(concat),
        "-c",
        "copy",
        str(root / "picture.mp4"),
    ]
    premaster = ["ffmpeg", "-nostdin", "-y"]
    for entry in manifest.audio_entries:
        premaster.extend(["-i", str(root / f"{entry.asset.sha256}.input")])
    filters: list[str] = []
    mix_labels: list[str] = []
    narration_label = "narration"
    for index, entry in enumerate(manifest.audio_entries):
        label = narration_label if entry.role == "narration" else f"stem{index}"
        samples = (entry.start_us * manifest.audio_profile.sample_rate_hz) // 1_000_000
        gain_db = f"{entry.gain_millidb / 1000:.3f}"
        chain = (
            f"[{index}:a]aresample={manifest.audio_profile.sample_rate_hz},"
            f"aformat=channel_layouts=stereo,volume={gain_db}dB"
        )
        if samples:
            chain += f",adelay={samples}S:all=1"
        filters.append(f"{chain}[{label}]")
        mix_labels.append(label)
    music = [
        (index, entry)
        for index, entry in enumerate(manifest.audio_entries)
        if entry.role == "music" and entry.duck_under_narration
    ]
    if music:
        narration_outputs = ["narration_mix", *(f"narration_sc{index}" for index, _ in music)]
        filters.append(
            f"[{narration_label}]asplit={len(narration_outputs)}"
            + "".join(f"[{label}]" for label in narration_outputs)
        )
        mix_labels[mix_labels.index(narration_label)] = "narration_mix"
    for index, _entry in music:
        source_label = f"stem{index}"
        ducked = f"ducked{index}"
        filters.append(
            f"[{source_label}][narration_sc{index}]"
            "sidechaincompress=threshold=0.0316228:ratio=6:attack=20:release=400"
            f"[{ducked}]"
        )
        mix_labels[mix_labels.index(source_label)] = ducked
    filters.append(
        "".join(f"[{label}]" for label in mix_labels)
        + f"amix=inputs={len(mix_labels)}:normalize=0,alimiter=limit=0.84[premaster]"
    )
    premaster.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[premaster]",
            "-c:a",
            "pcm_s24le",
            str(root / "premaster.wav"),
        ]
    )
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
    ]
    selectable = manifest.subtitle_mode in {"selectable", "both"}
    burn_in = manifest.subtitle_mode in {"burn_in", "both"}
    if selectable:
        final.extend(["-i", str(root / "captions.srt")])
    final.extend(["-map", "0:v:0", "-map", "1:a:0"])
    if selectable:
        final.extend(["-map", "2:s:0"])
    if burn_in:
        ass_path = str(root / "captions.ass")
        escaped_ass_path = (
            ass_path.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace(",", "\\,")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )
        final.extend(
            [
                "-vf",
                f"ass=filename='{escaped_ass_path}'",
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-x264-params",
                "bframes=0:scenecut=0",
            ]
        )
    else:
        final.extend(["-c:v", "copy"])
    final.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            f"{manifest.audio_profile.bitrate_kbps}k",
            "-ar",
            "48000",
        ]
    )
    if selectable:
        final.extend(
            [
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=eng",
            ]
        )
    final.extend(
        [
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(root / "final.mp4"),
        ]
    )
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
