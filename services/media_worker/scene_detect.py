from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

from services.media_worker.commands import CommandRunner
from vidgen.contracts.media import SceneBoundary, SceneDetectionResult

PTS_TIME = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def detect_scenes(
    source: Path,
    duration_seconds: float,
    threshold: float = 0.30,
    runner: CommandRunner | None = None,
) -> SceneDetectionResult:
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(source),
            "-filter:v",
            f"select=gt(scene\\,{threshold}),showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    cuts = sorted(
        {
            round(float(match.group(1)), 6)
            for match in PTS_TIME.finditer(result.stderr)
            if 0.05 < float(match.group(1)) < duration_seconds - 0.05
        }
    )
    boundaries = [0.0, *cuts, duration_seconds]
    scenes = [
        SceneBoundary(
            sequence=index,
            start_seconds=start,
            end_seconds=end,
            confidence=1.0 if index == 0 else threshold,
        )
        for index, (start, end) in enumerate(pairwise(boundaries))
        if end - start > 0.01
    ]
    return SceneDetectionResult(
        threshold=threshold,
        duration_seconds=duration_seconds,
        scenes=scenes,
    )
