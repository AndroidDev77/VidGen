"""FFprobe execution and canonical technical metadata extraction."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path

from vidgen.contracts.animation import VideoCodec, VideoContainer, VideoProbeResult


def probe_video(path: Path) -> VideoProbeResult:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-count_frames",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise ValueError(f"ffprobe_failed: {completed.stderr.strip()[:512]}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload["streams"]
        format_data = payload["format"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("ffprobe_failed: malformed probe output") from error
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise ValueError("unexpected_video_streams: exactly one video stream is required")
    video = videos[0]
    if "mp4" not in str(format_data.get("format_name", "")):
        raise ValueError("unsupported_container: expected MP4 provider output")
    try:
        duration = float(video.get("duration") or format_data["duration"])
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("ffprobe_failed: missing finite geometry or duration") from error
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("ffprobe_failed: non-finite or non-positive metadata")
    frame_rate = str(video.get("avg_frame_rate") or "0/0")
    timebase = str(video.get("time_base") or "0/0")
    if frame_rate in {"0/0", "N/A"} or timebase in {"0/0", "N/A"}:
        raise ValueError("ffprobe_failed: invalid frame rate or timebase")
    frame_text = video.get("nb_read_frames") or video.get("nb_frames")
    frame_count = int(frame_text) if frame_text not in {None, "N/A"} else None
    version = subprocess.run(
        ["ffprobe", "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    return VideoProbeResult(
        container=VideoContainer.MP4,
        video_codec=VideoCodec(str(video.get("codec_name", ""))),
        audio_codec=str(audios[0].get("codec_name")) if audios else None,
        width=width,
        height=height,
        display_aspect_ratio=str(video.get("display_aspect_ratio") or f"{width}:{height}"),
        pixel_format=str(video.get("pix_fmt", "")),
        frame_rate=frame_rate,
        timebase=timebase,
        duration_seconds=duration,
        frame_count=frame_count,
        byte_size=path.stat().st_size,
        sha256=_sha256(path),
        ffprobe_json=payload,
        ffprobe_version=version,
    )


def verify_boundary_decode(path: Path, duration_seconds: float) -> None:
    for position in (0.0, max(0.0, duration_seconds - 0.05)):
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{position:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise ValueError("decode_failed: first or last video frame is not decodable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
