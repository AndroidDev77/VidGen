from __future__ import annotations

from pathlib import Path

from services.media_worker.commands import CommandRunner, ensure_output


def extract_transcription_audio(
    source: Path, destination: Path, runner: CommandRunner | None = None
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
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-fflags",
            "+bitexact",
            str(destination),
        ]
    )
    ensure_output(destination)
    return destination
