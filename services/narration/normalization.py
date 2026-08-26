"""Deterministic FFmpeg normalization and FFprobe measurement."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioProbe:
    duration_seconds: float
    codec: str
    container: str
    sample_rate_hz: int
    channels: int
    bit_depth: int | None
    byte_size: int
    raw: dict[str, Any]


def normalize_audio(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(destination),
        ],
        check=True,
    )


def probe_audio(path: Path) -> AudioProbe:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(result.stdout)
    streams = [s for s in raw["streams"] if s.get("codec_type") == "audio"]
    if len(streams) != 1:
        raise ValueError("audio must contain exactly one audio stream")
    s = streams[0]
    duration = float(s.get("duration") or raw["format"]["duration"])
    if duration <= 0:
        raise ValueError("audio duration must be positive")
    return AudioProbe(
        duration,
        s["codec_name"],
        raw["format"]["format_name"],
        int(s["sample_rate"]),
        int(s["channels"]),
        int(s["bits_per_sample"]) or None,
        path.stat().st_size,
        raw,
    )
