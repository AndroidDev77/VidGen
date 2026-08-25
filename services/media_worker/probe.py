from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.media_worker.commands import CommandRunner
from vidgen.contracts.media import AudioStreamInfo, MediaProbeResult, VideoStreamInfo


def _rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0
    numerator, denominator = value.split("/", maxsplit=1)
    return float(numerator) / float(denominator)


def probe_media(path: Path, runner: CommandRunner | None = None) -> MediaProbeResult:
    command_runner = runner or CommandRunner()
    result = command_runner.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    payload: dict[str, Any] = json.loads(result.stdout)
    format_payload = payload.get("format")
    if isinstance(format_payload, dict):
        format_payload.pop("filename", None)
    streams = payload.get("streams", [])
    video_data = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_data is None:
        raise ValueError("source has no video stream")
    format_data = payload.get("format", {})
    duration = float(format_data.get("duration") or video_data.get("duration") or 0)
    video = VideoStreamInfo(
        codec=str(video_data.get("codec_name") or "unknown"),
        width=int(video_data["width"]),
        height=int(video_data["height"]),
        frame_rate=_rate(video_data.get("avg_frame_rate") or video_data.get("r_frame_rate")),
        pixel_format=video_data.get("pix_fmt"),
    )
    audio = [
        AudioStreamInfo(
            codec=str(stream.get("codec_name") or "unknown"),
            sample_rate=int(stream["sample_rate"]) if stream.get("sample_rate") else None,
            channels=int(stream["channels"]) if stream.get("channels") else None,
        )
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    return MediaProbeResult(
        duration_seconds=duration,
        format_name=str(format_data.get("format_name") or "unknown"),
        byte_size=int(format_data.get("size") or path.stat().st_size),
        video=video,
        audio_streams=audio,
        raw_probe=payload,
    )
