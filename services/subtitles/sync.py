from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from services.media_worker.commands import CommandRunner, ensure_output

CORRELATION = re.compile(r"correlation[:\s=]+([0-9.-]+)", re.IGNORECASE)
OFFSET = re.compile(r"offset(?:_seconds)?[:\s=]+([0-9.-]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SubtitleSyncResult:
    path: Path
    offset_seconds: float | None
    correlation: float | None
    synchronized: bool


def synchronize_subtitle(
    video: Path,
    subtitle: Path,
    destination: Path,
    *,
    enabled: bool = True,
    runner: CommandRunner | None = None,
) -> SubtitleSyncResult:
    if not enabled:
        return SubtitleSyncResult(subtitle, None, None, False)
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffsubsync",
            str(video),
            "-i",
            str(subtitle),
            "-o",
            str(destination),
            "--vad",
            "subs_then_webrtc",
        ],
        timeout_seconds=600,
    )
    ensure_output(destination)
    output = result.stdout + "\n" + result.stderr
    correlation_match = CORRELATION.search(output)
    offset_match = OFFSET.search(output)
    return SubtitleSyncResult(
        destination,
        float(offset_match.group(1)) if offset_match else None,
        float(correlation_match.group(1)) if correlation_match else None,
        True,
    )
