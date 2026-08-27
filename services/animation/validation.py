"""Deterministic technical-only video validation."""

from __future__ import annotations

from pathlib import Path

from services.animation.probe import probe_video, verify_boundary_decode
from vidgen.contracts.animation import (
    VideoValidationDiagnostic,
    VideoValidationReport,
)


def validate_video(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    requested_duration: float,
    minimum_usable_duration: float,
    duration_tolerance: float = 0.25,
    max_bytes: int = 512 * 1024 * 1024,
) -> VideoValidationReport:
    diagnostics: list[VideoValidationDiagnostic] = []
    try:
        probe = probe_video(path)
        verify_boundary_decode(path, probe.duration_seconds)
    except ValueError as error:
        return VideoValidationReport(
            valid=False,
            diagnostics=[
                VideoValidationDiagnostic(
                    code="video_probe_failed", severity="error", message=str(error)
                )
            ],
        )
    if "mp4" not in probe.container and "mov" not in probe.container:
        diagnostics.append(_error("unsupported_container", probe.container))
    if probe.video_codec not in {"h264", "hevc"}:
        diagnostics.append(_error("unsupported_codec", probe.video_codec))
    if (probe.width, probe.height) != (expected_width, expected_height):
        diagnostics.append(
            _error(
                "incorrect_dimensions",
                f"expected {expected_width}x{expected_height}, got {probe.width}x{probe.height}",
            )
        )
    if probe.audio_codec is not None:
        diagnostics.append(_error("unexpected_audio_stream", probe.audio_codec))
    if probe.frame_count is not None and probe.frame_count <= 0:
        diagnostics.append(_error("empty_video", "video has no frames"))
    if probe.duration_seconds + duration_tolerance < minimum_usable_duration:
        diagnostics.append(_error("duration_too_short", "video cannot cover T13 usable interval"))
    if abs(probe.duration_seconds - requested_duration) > duration_tolerance:
        diagnostics.append(
            _error(
                "incorrect_duration",
                f"expected {requested_duration}, measured {probe.duration_seconds}",
            )
        )
    if probe.byte_size > max_bytes:
        diagnostics.append(_error("video_too_large", str(probe.byte_size)))
    return VideoValidationReport(
        valid=not any(item.severity == "error" for item in diagnostics),
        probe=probe,
        diagnostics=diagnostics,
    )


def _error(code: str, message: str) -> VideoValidationDiagnostic:
    return VideoValidationDiagnostic(code=code, severity="error", message=message)
