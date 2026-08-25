from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaCommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str


class CommandRunner:
    def run(self, arguments: list[str], timeout_seconds: int = 300) -> CommandResult:
        if not arguments or not all(isinstance(argument, str) for argument in arguments):
            raise ValueError("command must be a non-empty string argument array")
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            stderr = error.stderr if isinstance(error.stderr, str) else ""
            raise MediaCommandError(f"media command failed: {stderr[-2000:]}") from error
        return CommandResult(result.stdout, result.stderr)


def require_media_tools(runner: CommandRunner) -> None:
    runner.run(["ffmpeg", "-version"], timeout_seconds=10)
    runner.run(["ffprobe", "-version"], timeout_seconds=10)


def ensure_output(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaCommandError(f"media command did not produce {path.name}")
