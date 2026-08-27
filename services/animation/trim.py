"""Deterministic T13-authoritative trimming."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from services.animation.probe import probe_video
from vidgen.contracts.animation import VideoProbeResult, VideoTrimManifest


@dataclass(frozen=True, slots=True)
class TrimmedVideo:
    path: Path
    manifest: VideoTrimManifest
    probe: VideoProbeResult
    ffmpeg_version: str
    input_sha256: str
    output_sha256: str


def trim_video(
    source: Path,
    *,
    trim_in_seconds: float,
    trim_out_seconds: float,
    usable_duration_seconds: float,
    frame_tolerance_seconds: float = 1 / 24,
) -> TrimmedVideo:
    if min(trim_in_seconds, trim_out_seconds) < 0 or usable_duration_seconds <= 0:
        raise ValueError("invalid T13 trim manifest")
    temporary = NamedTemporaryFile(prefix="vidgen-trim-", suffix=".mp4", delete=False)
    temporary.close()
    output = Path(temporary.name)
    arguments = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{trim_in_seconds:.6f}",
        "-i",
        str(source),
        "-t",
        f"{usable_duration_seconds:.6f}",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]
    completed = subprocess.run(arguments, capture_output=True, check=False)
    if completed.returncode:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg trim failed: {completed.stderr.decode(errors='replace')[:512]}")
    probe = probe_video(output)
    if abs(probe.duration_seconds - usable_duration_seconds) > frame_tolerance_seconds:
        output.unlink(missing_ok=True)
        raise ValueError("trimmed duration differs from T13 usable duration")
    version = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    manifest = VideoTrimManifest(
        trim_in_seconds=trim_in_seconds,
        trim_out_seconds=trim_out_seconds,
        usable_duration_seconds=usable_duration_seconds,
        ffmpeg_arguments=[
            "<input>" if value == str(source) else "<output>" if value == str(output) else value
            for value in arguments
        ],
        encoding_profile="h264-crf18-yuv420p-threads1-v1",
    )
    return TrimmedVideo(
        output,
        manifest,
        probe,
        version,
        _sha256(source),
        _sha256(output),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
