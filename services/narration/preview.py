"""Streaming deterministic preview concatenation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from services.narration.normalization import AudioProbe, probe_audio


def concatenate_preview(inputs: list[Path], destination: Path, workdir: Path) -> AudioProbe:
    if not inputs:
        raise ValueError("preview requires at least one segment")
    manifest = workdir / "concat.txt"
    # Paths are generated temporary paths, never user-controlled. Quote FFmpeg demuxer syntax.
    manifest.write_text(
        "".join(f"file '{p.as_posix().replace(chr(39), chr(39) * 3)}'\n" for p in inputs)
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-map_metadata",
            "-1",
            "-c:a",
            "copy",
            str(destination),
        ],
        check=True,
    )
    return probe_audio(destination)
