"""Deterministic media checks for the assembled T22 final render.

These checks run before any paid request and answer one question: is the file
the renderer produced actually a valid, complete delivery of the canonical
timeline? They operate on the *assembled* output, so they detect damage
introduced during concatenation, filtering, trimming, captioning, mixing or
encoding - problems that no shot-level T20 result can see.

The T20 shot-level scoring is deliberately not reused or altered here. What is
reused are the repository's existing measurement utilities: ``ffprobe`` JSON,
whole-file decode, the black/freeze/silence detectors, and the tool-version
helper, all invoked through safe subprocess argument arrays. No command string
is ever constructed from a manifest, an asset name or any other value that
originates outside this module.

A failed deterministic check is always blocking and can never be overridden by a
provider score or a human decision.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

from services.qa.deterministic import tool_version
from services.qa.final_evidence import deterministic_id
from services.qa.final_rubric import DETERMINISTIC_CHECK_VERSION
from services.renderer.verify import (
    RenderVerificationError,
    diagnostic_intervals,
    probe,
    run_bounded,
)
from vidgen.contracts.final_editorial import (
    FinalCheckType,
    FinalDeterministicCheck,
    FinalIssueCode,
    FinalMediaMeasurements,
    FinalQAConfiguration,
    FinalQAInput,
)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _deterministic_check_id(*parts: object) -> UUID:
    """A stable ID so a resumed run produces the identical check row."""
    return deterministic_id("check", *parts)


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _check(
    code: FinalIssueCode,
    status: str,
    *,
    check_type: FinalCheckType = FinalCheckType.MEDIA,
    measurement: float | None = None,
    threshold: float | None = None,
    unit: str = "",
    start_us: int | None = None,
    end_us: int | None = None,
    tool: str = FFPROBE,
    tool_version_string: str = "",
    message: str = "",
    identity: object = "",
) -> FinalDeterministicCheck:
    return FinalDeterministicCheck(
        check_id=_deterministic_check_id(identity, code.value),
        check_type=check_type,
        check_version=DETERMINISTIC_CHECK_VERSION,
        code=code,
        status=status,  # type: ignore[arg-type]
        blocking=status == "fail",
        measurement=measurement,
        threshold=threshold,
        unit=unit,
        start_us=start_us,
        end_us=end_us,
        tool=tool,
        tool_version=tool_version_string,
        message=message[:500],
    )


def decode_stream(path: Path, stream: str, *, timeout: int = 900) -> tuple[bool, int, str]:
    """Fully decode one stream kind and count the decoder's reported errors."""
    result = run_bounded(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            f"0:{stream}",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    errors = len([line for line in result.stderr.splitlines() if line.strip()])
    return result.returncode == 0 and errors == 0, errors, result.stderr[-2000:]


def boundary_frame_valid(path: Path, timestamp_us: int, *, timeout: int = 180) -> bool:
    """Decode exactly one frame at a timestamp; a delivery must have both ends."""
    result = run_bounded(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{max(timestamp_us, 0) / 1_000_000:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            # Decode the frame and discard it. Writing raw pixels to a pipe
            # would hand binary bytes to a text-capturing runner.
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    return result.returncode == 0


def monotonic_timestamps(path: Path, *, packet_limit: int = 20000) -> tuple[bool, int]:
    """Verify presentation timestamps never travel backwards, and find the start."""
    result = run_bounded(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,dts_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    if result.returncode:
        raise RenderVerificationError("packet timestamp inspection failed")
    previous: float | None = None
    first: float | None = None
    monotonic = True
    for index, line in enumerate(result.stdout.splitlines()):
        if index >= packet_limit:
            break
        raw = line.split(",")[0].strip()
        value = _finite(raw)
        if value is None:
            continue
        if first is None:
            first = value
        if previous is not None and value + 1e-6 < previous:
            monotonic = False
            break
        previous = value
    return monotonic, round((first or 0.0) * 1_000_000)


def _stream_duration_us(stream: dict[str, Any], container_duration_us: int | None) -> int | None:
    duration = _finite(stream.get("duration"))
    if duration is not None:
        return round(duration * 1_000_000)
    tags = stream.get("tags")
    if isinstance(tags, dict):
        raw = tags.get("DURATION")
        if isinstance(raw, str):
            parts = raw.split(":")
            if len(parts) == 3:
                seconds = _finite(parts[2])
                if seconds is not None:
                    hours = _finite(parts[0]) or 0.0
                    minutes = _finite(parts[1]) or 0.0
                    return round((hours * 3600 + minutes * 60 + seconds) * 1_000_000)
    return container_duration_us


def measure(path: Path, configuration: FinalQAConfiguration) -> FinalMediaMeasurements:
    """Probe and decode the assembled render, recording only finite measurements."""
    metadata = probe(path)
    streams = [item for item in metadata.get("streams", []) if isinstance(item, dict)]
    container = metadata.get("format", {})
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]
    container_duration = _finite(container.get("duration"))
    container_duration_us = None if container_duration is None else round(container_duration * 1e6)

    video_ok, video_errors, _ = decode_stream(path, "v") if video is not None else (False, 1, "")
    audio_ok, audio_errors, _ = decode_stream(path, "a") if audio is not None else (False, 1, "")
    monotonic, video_start_us = (True, 0)
    if video is not None:
        monotonic, video_start_us = monotonic_timestamps(path)

    black = diagnostic_intervals(path, "black") if video is not None else []
    freeze = diagnostic_intervals(path, "freeze") if video is not None else []
    silence = diagnostic_intervals(path, "silence") if audio is not None else []

    last_us = max((container_duration_us or 1) - 60_000, 0)
    return FinalMediaMeasurements(
        measured_at=datetime.now(UTC),
        ffmpeg_version=tool_version(FFMPEG),
        ffprobe_version=tool_version(FFPROBE),
        container_format=str(container.get("format_name", ""))[:128],
        byte_size=int(_finite(container.get("size")) or path.stat().st_size),
        bit_rate=_finite(container.get("bit_rate")),
        video_codec=str((video or {}).get("codec_name", ""))[:64],
        audio_codec=str((audio or {}).get("codec_name", ""))[:64],
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        pixel_format=str((video or {}).get("pix_fmt", ""))[:32],
        frame_rate=str((video or {}).get("avg_frame_rate", ""))[:32],
        video_time_base=str((video or {}).get("time_base", ""))[:32],
        audio_time_base=str((audio or {}).get("time_base", ""))[:32],
        container_duration_us=container_duration_us,
        video_duration_us=(
            _stream_duration_us(video, container_duration_us) if video is not None else None
        ),
        audio_duration_us=(
            _stream_duration_us(audio, container_duration_us) if audio is not None else None
        ),
        video_start_us=max(video_start_us, 0),
        audio_start_us=max(round((_finite((audio or {}).get("start_time")) or 0.0) * 1e6), 0),
        sample_rate_hz=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        subtitle_stream_count=len(subtitles),
        video_decoded=video_ok,
        audio_decoded=audio_ok,
        monotonic_video_timestamps=monotonic,
        first_frame_valid=boundary_frame_valid(path, 0) if video is not None else False,
        last_frame_valid=boundary_frame_valid(path, last_us) if video is not None else False,
        black_intervals=black[:512],
        freeze_intervals=freeze[:512],
        silence_intervals=silence[:512],
        decode_error_count=video_errors + audio_errors,
    )


def _frame_rate_valid(frame_rate: str, expected: int) -> bool:
    if frame_rate in {f"{expected}/1", str(expected)}:
        return True
    if "/" not in frame_rate:
        return False
    numerator, _, denominator = frame_rate.partition("/")
    top, bottom = _finite(numerator), _finite(denominator)
    if top is None or bottom is None or bottom == 0:
        return False
    return abs(top / bottom - expected) <= 0.01


def _expected_shot_intervals(inputs: FinalQAInput) -> list[tuple[int, int]]:
    return [(shot.global_start_us, shot.global_end_us) for shot in inputs.shots]


def evaluate(
    measurements: FinalMediaMeasurements,
    inputs: FinalQAInput,
    configuration: FinalQAConfiguration,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[FinalDeterministicCheck]:
    """Grade the measured render against the delivery profile and the manifest."""
    identity = inputs.render_identity
    versions = {"ffmpeg": measurements.ffmpeg_version, "ffprobe": measurements.ffprobe_version}
    checks: list[FinalDeterministicCheck] = []

    def add(code: FinalIssueCode, ok: bool, **kwargs: Any) -> None:
        kwargs.setdefault("identity", identity)
        kwargs.setdefault("tool_version_string", versions["ffprobe"])
        checks.append(_check(code, "pass" if ok else "fail", **kwargs))

    add(
        FinalIssueCode.RENDER_EMPTY,
        measurements.byte_size > 0,
        measurement=float(measurements.byte_size),
        unit="bytes",
        message="the final render must exist and be nonempty",
    )
    add(
        FinalIssueCode.CONTAINER_MISMATCH,
        configuration.expected_container in measurements.container_format.split(","),
        message=(
            f"expected a {configuration.expected_container} container, "
            f"measured {measurements.container_format or 'none'}"
        ),
    )
    add(
        FinalIssueCode.MISSING_VIDEO_STREAM,
        bool(measurements.video_codec),
        message="the delivery requires exactly one video stream",
    )
    add(
        FinalIssueCode.MISSING_AUDIO_STREAM,
        bool(measurements.audio_codec),
        message="the delivery requires exactly one audio stream",
    )
    add(
        FinalIssueCode.VIDEO_CODEC_MISMATCH,
        measurements.video_codec == configuration.expected_video_codec,
        message=(
            f"expected {configuration.expected_video_codec}, "
            f"measured {measurements.video_codec or 'none'}"
        ),
    )
    add(
        FinalIssueCode.AUDIO_CODEC_MISMATCH,
        measurements.audio_codec == configuration.expected_audio_codec,
        message=(
            f"expected {configuration.expected_audio_codec}, "
            f"measured {measurements.audio_codec or 'none'}"
        ),
    )
    expected_subtitles = 0 if inputs.subtitle_mode == "burn_in" else 1
    add(
        FinalIssueCode.UNEXPECTED_STREAM,
        measurements.subtitle_stream_count == expected_subtitles,
        measurement=float(measurements.subtitle_stream_count),
        threshold=float(expected_subtitles),
        message=(
            f"subtitle mode {inputs.subtitle_mode} expects "
            f"{expected_subtitles} selectable subtitle stream(s)"
        ),
    )
    add(
        FinalIssueCode.VIDEO_DECODE_FAILURE,
        measurements.video_decoded,
        measurement=float(measurements.decode_error_count),
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the video stream must decode completely",
    )
    add(
        FinalIssueCode.AUDIO_DECODE_FAILURE,
        measurements.audio_decoded,
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the audio stream must decode completely",
    )
    add(
        FinalIssueCode.RESOLUTION_MISMATCH,
        (measurements.width, measurements.height)
        == (configuration.expected_width, configuration.expected_height),
        message=(
            f"expected {configuration.expected_width}x{configuration.expected_height}, "
            f"measured {measurements.width}x{measurements.height}"
        ),
    )
    add(
        FinalIssueCode.PIXEL_FORMAT_MISMATCH,
        measurements.pixel_format == configuration.expected_pixel_format,
        message=(
            f"expected {configuration.expected_pixel_format}, "
            f"measured {measurements.pixel_format or 'none'}"
        ),
    )
    add(
        FinalIssueCode.FRAME_RATE_INVALID,
        _frame_rate_valid(measurements.frame_rate, configuration.expected_frame_rate),
        message=(
            f"expected {configuration.expected_frame_rate} fps, "
            f"measured {measurements.frame_rate or 'none'}"
        ),
    )
    add(
        FinalIssueCode.TIME_BASE_INVALID,
        bool(measurements.video_time_base) and "/" in measurements.video_time_base,
        message=f"invalid video time base {measurements.video_time_base or 'none'}",
    )
    duration = measurements.container_duration_us
    add(
        FinalIssueCode.DURATION_INVALID,
        duration is not None and duration > 0,
        measurement=None if duration is None else float(duration),
        unit="us",
        message="the final render duration must be finite and positive",
    )

    expected_duration = inputs.timeline_duration_us
    tolerance = configuration.duration_tolerance_us
    if measurements.video_duration_us is not None:
        drift = abs(measurements.video_duration_us - expected_duration)
        add(
            FinalIssueCode.VIDEO_DURATION_MISMATCH,
            drift <= tolerance,
            measurement=float(drift),
            threshold=float(tolerance),
            unit="us",
            message=f"video duration differs from the render manifest by {drift} us",
        )
    if measurements.audio_duration_us is not None:
        drift = abs(measurements.audio_duration_us - expected_duration)
        add(
            FinalIssueCode.AUDIO_DURATION_MISMATCH,
            drift <= tolerance,
            measurement=float(drift),
            threshold=float(tolerance),
            unit="us",
            message=f"audio duration differs from the render manifest by {drift} us",
        )
    if measurements.video_duration_us is not None and measurements.audio_duration_us is not None:
        drift = abs(measurements.video_duration_us - measurements.audio_duration_us)
        add(
            FinalIssueCode.AV_DURATION_DRIFT,
            drift <= configuration.av_drift_tolerance_us,
            measurement=float(drift),
            threshold=float(configuration.av_drift_tolerance_us),
            unit="us",
            message=f"audio and video durations drift by {drift} us",
        )
    if duration is not None:
        drift = abs(duration - expected_duration)
        add(
            FinalIssueCode.TIMELINE_DURATION_MISMATCH,
            drift <= tolerance,
            measurement=float(drift),
            threshold=float(tolerance),
            unit="us",
            message=f"final duration differs from the canonical timeline by {drift} us",
        )

    intervals = _expected_shot_intervals(inputs)
    gaps = [
        (previous[1], current[0])
        for previous, current in pairwise(intervals)
        if current[0] > previous[1]
    ]
    overlaps = [
        (current[0], previous[1])
        for previous, current in pairwise(intervals)
        if current[0] < previous[1]
    ]
    add(
        FinalIssueCode.SHOT_COVERAGE_GAP,
        not gaps,
        measurement=float(len(gaps)),
        start_us=gaps[0][0] if gaps else None,
        end_us=gaps[0][1] if gaps else None,
        message="shot intervals must cover the timeline without unexplained gaps",
    )
    add(
        FinalIssueCode.SHOT_COVERAGE_OVERLAP,
        not overlaps,
        measurement=float(len(overlaps)),
        start_us=overlaps[0][0] if overlaps else None,
        end_us=overlaps[0][1] if overlaps else None,
        message="shot intervals must not overlap outside declared transitions",
    )

    if manifest is not None:
        checks.append(_transition_check(manifest, identity, versions["ffprobe"]))

    black = _first_excessive(measurements.black_intervals, configuration.max_black_interval_us)
    add(
        FinalIssueCode.UNEXPECTED_BLACK_INTERVAL,
        black is None,
        measurement=None if black is None else float(black[1] - black[0]),
        threshold=float(configuration.max_black_interval_us),
        unit="us",
        start_us=None if black is None else black[0],
        end_us=None if black is None else black[1],
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the assembled render contains an unexpected black interval",
    )
    freeze = _first_excessive(measurements.freeze_intervals, configuration.max_freeze_interval_us)
    add(
        FinalIssueCode.EXCESSIVE_FREEZE_INTERVAL,
        freeze is None,
        measurement=None if freeze is None else float(freeze[1] - freeze[0]),
        threshold=float(configuration.max_freeze_interval_us),
        unit="us",
        start_us=None if freeze is None else freeze[0],
        end_us=None if freeze is None else freeze[1],
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the assembled render contains an excessive frozen-frame interval",
    )
    add(
        FinalIssueCode.CORRUPT_RENDER_SECTION,
        measurements.decode_error_count == 0,
        measurement=float(measurements.decode_error_count),
        threshold=0.0,
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the decoder reported damaged or repeated render sections",
    )
    add(
        FinalIssueCode.INVALID_BOUNDARY_FRAME,
        measurements.first_frame_valid and measurements.last_frame_valid,
        tool=FFMPEG,
        tool_version_string=versions["ffmpeg"],
        message="the first and final frames must both decode",
    )
    add(
        FinalIssueCode.NON_MONOTONIC_TIMESTAMPS,
        measurements.monotonic_video_timestamps,
        message="stream presentation timestamps must be monotonic",
    )
    add(
        FinalIssueCode.START_OFFSET_OUT_OF_RANGE,
        max(measurements.video_start_us, measurements.audio_start_us)
        <= configuration.start_offset_tolerance_us,
        measurement=float(max(measurements.video_start_us, measurements.audio_start_us)),
        threshold=float(configuration.start_offset_tolerance_us),
        unit="us",
        message="stream timestamps must start within the allowed offset",
    )
    add(
        FinalIssueCode.NON_FINITE_MEASUREMENT,
        measurements.bit_rate is None or math.isfinite(measurements.bit_rate),
        message="no non-finite media measurement may be accepted",
    )
    seconds = (duration or 0) / 1_000_000
    rate = measurements.byte_size / seconds if seconds > 0 else 0.0
    add(
        FinalIssueCode.FILE_SIZE_OUT_OF_RANGE,
        configuration.min_bytes_per_second <= rate <= configuration.max_bytes_per_second,
        measurement=rate,
        threshold=float(configuration.max_bytes_per_second),
        unit="bytes/s",
        message=f"delivery byte rate {rate:.0f} B/s is outside the configured limits",
    )
    if measurements.bit_rate is not None:
        add(
            FinalIssueCode.BITRATE_OUT_OF_RANGE,
            measurements.bit_rate <= configuration.max_bytes_per_second * 8,
            measurement=measurements.bit_rate,
            threshold=float(configuration.max_bytes_per_second * 8),
            unit="bit/s",
            message="container bitrate exceeds the configured delivery limit",
        )
    return checks


def _transition_check(
    manifest: dict[str, Any], identity: str, version: str
) -> FinalDeterministicCheck:
    """Every declared crossfade must carry the handles the manifest recorded."""
    mismatched: list[str] = []
    for shot in manifest.get("shots", []):
        if not isinstance(shot, dict):
            continue
        for side in ("transition_in", "transition_out"):
            transition = shot.get(side)
            if not isinstance(transition, dict):
                continue
            handles = int(transition.get("handle_in_us", 0)) + int(
                transition.get("handle_out_us", 0)
            )
            duration = int(transition.get("duration_us", 0))
            if transition.get("kind") == "crossfade" and (duration <= 0 or handles <= 0):
                mismatched.append(f"{shot.get('sequence')}:{side}")
            if transition.get("kind") == "cut" and (duration or handles):
                mismatched.append(f"{shot.get('sequence')}:{side}")
    return _check(
        FinalIssueCode.TRANSITION_HANDLE_MISMATCH,
        "pass" if not mismatched else "fail",
        measurement=float(len(mismatched)),
        tool=FFPROBE,
        tool_version_string=version,
        message=(
            "transition handles must match the render manifest: " + ", ".join(mismatched[:8])
            if mismatched
            else "transition handles match the render manifest"
        ),
        identity=identity,
    )


def _first_excessive(intervals: list[dict[str, int]], limit_us: int) -> tuple[int, int] | None:
    for interval in intervals:
        start, end = int(interval.get("start_us", 0)), int(interval.get("end_us", 0))
        if end - start > limit_us:
            return start, end
    return None
