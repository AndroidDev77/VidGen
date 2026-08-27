"""Bounded technical verification for final T17 media."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class RenderVerificationError(ValueError):
    pass


def run_bounded(
    arguments: list[str], *, timeout: int = 600, output_limit: int = 1_000_000
) -> subprocess.CompletedProcess[str]:
    if not arguments or not isinstance(arguments, list):
        raise TypeError("subprocess arguments must be a non-empty list")
    result = subprocess.run(arguments, check=False, text=True, capture_output=True, timeout=timeout)
    result.stdout = result.stdout[-output_limit:]
    result.stderr = result.stderr[-output_limit:]
    return result


def probe(path: Path) -> dict[str, Any]:
    result = run_bounded(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    )
    if result.returncode:
        raise RenderVerificationError("ffprobe failed")
    return dict(json.loads(result.stdout))


def verify_streams(
    metadata: dict[str, Any], *, fps: int, duration_us: int, tolerance_us: int = 80_000
) -> dict[str, Any]:
    streams = metadata.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle = [s for s in streams if s.get("codec_type") == "subtitle"]
    failures: list[str] = []
    if len(video) != 1 or video[0].get("codec_name") != "h264":
        failures.append("expected one H.264 video stream")
    elif (video[0].get("width"), video[0].get("height"), video[0].get("pix_fmt")) != (
        1920,
        1080,
        "yuv420p",
    ):
        failures.append("invalid video normalization")
    if (
        len(audio) != 1
        or audio[0].get("codec_name") != "aac"
        or int(audio[0].get("sample_rate", 0)) != 48000
    ):
        failures.append("expected one AAC 48 kHz audio stream")
    if len(subtitle) != 1 or subtitle[0].get("codec_name") not in {"mov_text", "tx3g"}:
        failures.append("expected selectable MP4 subtitle stream")
    measured = round(float(metadata.get("format", {}).get("duration", 0)) * 1_000_000)
    if abs(measured - duration_us) > tolerance_us:
        failures.append("duration outside tolerance")
    rate = video[0].get("avg_frame_rate") if video else None
    if rate not in {f"{fps}/1", str(fps)}:
        failures.append("unexpected frame rate")
    if failures:
        raise RenderVerificationError("; ".join(failures))
    return {
        "measured_duration_us": measured,
        "frame_rate": rate,
        "video": video[0],
        "audio": audio[0],
        "subtitle": subtitle[0],
    }


def decode_complete(path: Path, *, timeout: int = 600) -> None:
    result = run_bounded(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"], timeout=timeout
    )
    if result.returncode:
        raise RenderVerificationError("complete output decode failed")
