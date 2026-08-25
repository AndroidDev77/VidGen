from __future__ import annotations

import subprocess
from pathlib import Path

from services.media_worker.commands import CommandResult
from services.subtitles.embedded import (
    discover_embedded_subtitles,
    extract_embedded_subtitle,
)
from services.subtitles.parser import parse_subtitles


def test_discovers_and_extracts_embedded_text_subtitle(tmp_path: Path, golden_video: Path) -> None:
    sidecar = tmp_path / "episode.en.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nEmbedded hello\n")
    video = tmp_path / "with-subtitles.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(golden_video),
            "-i",
            str(sidecar),
            "-map",
            "0",
            "-map",
            "1",
            "-c",
            "copy",
            "-metadata:s:s:0",
            "language=eng",
            str(video),
        ],
        check=True,
    )
    candidates, warnings = discover_embedded_subtitles(video)
    assert warnings == []
    assert len(candidates) == 1
    assert candidates[0].language == "eng"
    output = extract_embedded_subtitle(video, candidates[0], tmp_path / "out.vtt")
    assert parse_subtitles(output.read_bytes(), "vtt")[0].text == "Embedded hello"


def test_untagged_embedded_subtitle_has_unknown_language(tmp_path: Path) -> None:
    video = tmp_path / "untagged.mkv"
    video.write_bytes(b"fixture")

    class FakeRunner:
        def run(self, arguments: list[str], timeout_seconds: int = 300) -> CommandResult:
            del arguments, timeout_seconds
            return CommandResult(
                '{"streams":[{"index":2,"codec_name":"subrip","tags":{"language":"und"}}]}',
                "",
            )

    candidates, warnings = discover_embedded_subtitles(video, FakeRunner())  # type: ignore[arg-type]
    assert warnings == []
    assert candidates[0].language is None
