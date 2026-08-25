from __future__ import annotations

from pathlib import Path

from services.media_worker.commands import CommandRunner, ensure_output


def extract_frame(
    source: Path,
    timestamp_seconds: float,
    destination: Path,
    runner: CommandRunner | None = None,
) -> Path:
    command_runner = runner or CommandRunner()
    command_runner.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp_seconds:.6f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-an",
            "-threads",
            "1",
            "-c:v",
            "png",
            "-fflags",
            "+bitexact",
            str(destination),
        ]
    )
    ensure_output(destination)
    return destination
