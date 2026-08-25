from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.media_worker.commands import CommandRunner, ensure_output
from vidgen.contracts.subtitles import SubtitleCandidate

TEXT_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"})


def discover_embedded_subtitles(
    video: Path, runner: CommandRunner | None = None
) -> tuple[list[SubtitleCandidate], list[str]]:
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_streams",
            "-of",
            "json",
            str(video),
        ]
    )
    payload = json.loads(result.stdout)
    candidates: list[SubtitleCandidate] = []
    warnings: list[str] = []
    for stream in payload.get("streams", []):
        codec = str(stream.get("codec_name") or "unknown").lower()
        index = int(stream["index"])
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = (
            stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        )
        if codec not in TEXT_CODECS:
            warnings.append(f"ignored bitmap or unsupported subtitle stream {index} ({codec})")
            continue
        language = str(tags.get("language") or "und").lower()
        material = f"{video.stat().st_size}:{index}:{codec}:{language}"
        candidate_id = "embedded_" + hashlib.sha256(material.encode()).hexdigest()[:24]
        candidates.append(
            SubtitleCandidate(
                candidate_id=candidate_id,
                source_type="embedded",
                provider="ffmpeg",
                stream_index=index,
                language=language,
                subtitle_format="webvtt",
                hearing_impaired="sdh" in str(tags.get("title") or "").lower(),
                forced=bool(disposition.get("forced")),
                file_name=f"stream-{index}.vtt",
                metadata={"codec": codec, "title": str(tags.get("title") or "")},
            )
        )
    return candidates, warnings


def extract_embedded_subtitle(
    video: Path,
    candidate: SubtitleCandidate,
    destination: Path,
    runner: CommandRunner | None = None,
) -> Path:
    if candidate.source_type != "embedded" or candidate.stream_index is None:
        raise ValueError("candidate is not an embedded subtitle stream")
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
            "-i",
            str(video),
            "-map",
            f"0:{candidate.stream_index}",
            "-map_metadata",
            "-1",
            "-c:s",
            "webvtt",
            "-f",
            "webvtt",
            str(destination),
        ]
    )
    ensure_output(destination)
    return destination
