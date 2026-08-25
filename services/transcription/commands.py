from __future__ import annotations

import json
import re
from pathlib import Path

from services.media_worker.commands import CommandRunner, ensure_output

SILENCE_START = re.compile(r"silence_start: ([0-9]+(?:\.[0-9]+)?)")
SILENCE_END = re.compile(r"silence_end: ([0-9]+(?:\.[0-9]+)?)")


def probe_duration(source: Path, runner: CommandRunner | None = None) -> float:
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        ]
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if duration <= 0:
        raise ValueError("audio duration must be positive")
    return duration


def detect_silence_ranges(
    source: Path,
    *,
    duration_seconds: float,
    noise_db: float = -38,
    minimum_silence_seconds: float = 0.35,
    runner: CommandRunner | None = None,
) -> list[tuple[float, float]]:
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={minimum_silence_seconds}",
            "-f",
            "null",
            "-",
        ]
    )
    starts = [float(value) for value in SILENCE_START.findall(result.stderr)]
    ends = [float(value) for value in SILENCE_END.findall(result.stderr)]
    ranges: list[tuple[float, float]] = []
    end_index = 0
    for start in starts:
        while end_index < len(ends) and ends[end_index] <= start:
            end_index += 1
        end = ends[end_index] if end_index < len(ends) else duration_seconds
        ranges.append((max(0.0, start), min(duration_seconds, end)))
        end_index += 1
    return ranges


def encode_flac(
    source: Path,
    destination: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    sample_rate: int,
    runner: CommandRunner | None = None,
) -> None:
    command_runner = runner or CommandRunner()
    destination.parent.mkdir(parents=True, exist_ok=True)
    command_runner.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_seconds:.6f}",
            "-to",
            f"{end_seconds:.6f}",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "flac",
            "-compression_level",
            "12",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(destination),
        ]
    )
    ensure_output(destination)
