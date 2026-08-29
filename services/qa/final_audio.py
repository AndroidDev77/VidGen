"""Deterministic audio-mix checks for the assembled T22 final render.

Everything here is measured, never judged. Loudness and true peak come from
``loudnorm``; peak, RMS and clipped-sample counts come from ``astats``; silence
comes from ``silencedetect``. Narration coverage is checked by intersecting the
approved T12 word timings with the measured non-silent intervals of the delivered
mix, so an omitted, duplicated or drifting narration segment is located by an
exact global timestamp range rather than inferred from a score.

Every check records its measurement, the threshold it was compared against, the
tool and tool version that produced it, and the affected time range. A failed
audio check is blocking and is never overridable.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from services.qa.deterministic import tool_version
from services.qa.final_evidence import deterministic_id
from services.qa.final_rubric import AUDIO_CHECK_VERSION
from services.renderer.audio import parse_loudnorm_json
from services.renderer.verify import RenderVerificationError, run_bounded
from vidgen.contracts.final_editorial import (
    FinalAudioCheck,
    FinalIssueCode,
    FinalMediaMeasurements,
    FinalQAConfiguration,
    FinalQAInput,
)

FFMPEG = "ffmpeg"

_ASTATS = re.compile(r"lavfi\.astats\.Overall\.(?P<key>[A-Za-z_]+)=(?P<value>-?[0-9.eE+]+|inf|nan)")


def _audio_check(
    code: FinalIssueCode,
    ok: bool,
    *,
    identity: str,
    measurement: float | None = None,
    threshold: float | None = None,
    unit: str = "",
    start_us: int | None = None,
    end_us: int | None = None,
    tool: str = FFMPEG,
    tool_version_string: str = "",
    message: str = "",
    narration_segment_id: UUID | None = None,
    suffix: str = "",
) -> FinalAudioCheck:
    return FinalAudioCheck(
        check_id=deterministic_id("audio-check", identity, code.value, suffix),
        check_version=AUDIO_CHECK_VERSION,
        code=code,
        status="pass" if ok else "fail",
        blocking=not ok,
        measurement=measurement,
        threshold=threshold,
        unit=unit,
        start_us=start_us,
        end_us=end_us,
        tool=tool,
        tool_version=tool_version_string,
        message=message[:500],
        narration_segment_id=narration_segment_id,
    )


def measure_loudness(path: Path, *, timeout: int = 900) -> dict[str, float]:
    """Integrated loudness, true peak and loudness range of the delivered mix."""
    result = run_bounded(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    if result.returncode:
        raise RenderVerificationError("loudness measurement failed")
    return parse_loudnorm_json(result.stderr)


def measure_statistics(path: Path, *, timeout: int = 900) -> dict[str, float]:
    """Peak level, RMS and the decoder's clipped-sample count, all finite."""
    result = run_bounded(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "astats=metadata=1:reset=0,ametadata=mode=print:file=-",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    if result.returncode:
        raise RenderVerificationError("audio statistics measurement failed")
    statistics: dict[str, float] = {}
    for match in _ASTATS.finditer(result.stdout + result.stderr):
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isfinite(value):
            statistics[match.group("key")] = value
    return statistics


def clipping_ratio(statistics: dict[str, float]) -> float:
    """Clipped samples over total samples, or zero when nothing was measured."""
    total = statistics.get("Number_of_samples", 0.0)
    clipped = statistics.get("Number_of_clipped_samples", 0.0)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, clipped / total))


def non_silent_intervals(
    measurements: FinalMediaMeasurements, duration_us: int
) -> list[tuple[int, int]]:
    """The complement of the measured silence intervals, clamped to the timeline."""
    silence = sorted(
        (int(item.get("start_us", 0)), int(item.get("end_us", 0)))
        for item in measurements.silence_intervals
    )
    intervals: list[tuple[int, int]] = []
    cursor = 0
    for start, end in silence:
        if start > cursor:
            intervals.append((cursor, min(start, duration_us)))
        cursor = max(cursor, end)
    if cursor < duration_us:
        intervals.append((cursor, duration_us))
    return [(start, end) for start, end in intervals if end > start]


def _covered(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    """Microseconds of ``[start, end)`` covered by any non-silent interval."""
    total = 0
    for low, high in intervals:
        overlap = min(end, high) - max(start, low)
        if overlap > 0:
            total += overlap
    return total


def leading_silence_us(measurements: FinalMediaMeasurements) -> int:
    for interval in measurements.silence_intervals:
        if int(interval.get("start_us", 0)) <= 1000:
            return int(interval.get("end_us", 0))
    return 0


def trailing_silence_us(measurements: FinalMediaMeasurements, duration_us: int) -> int:
    for interval in measurements.silence_intervals:
        if int(interval.get("end_us", 0)) >= duration_us - 1000:
            return duration_us - int(interval.get("start_us", 0))
    return 0


def evaluate(
    path: Path,
    inputs: FinalQAInput,
    configuration: FinalQAConfiguration,
    measurements: FinalMediaMeasurements,
    *,
    narration_intervals: list[tuple[UUID, int, int]],
    manifest: dict[str, Any] | None = None,
    loudness: dict[str, float] | None = None,
    statistics: dict[str, float] | None = None,
) -> tuple[list[FinalAudioCheck], dict[str, float]]:
    """Grade the final mix; returns the checks and the measurements they used."""
    identity = inputs.render_identity
    version = tool_version(FFMPEG)
    loudness = loudness if loudness is not None else measure_loudness(path)
    statistics = statistics if statistics is not None else measure_statistics(path)
    duration = inputs.timeline_duration_us
    audible = non_silent_intervals(measurements, duration)
    checks: list[FinalAudioCheck] = []

    def add(code: FinalIssueCode, ok: bool, **kwargs: Any) -> None:
        kwargs.setdefault("identity", identity)
        kwargs.setdefault("tool_version_string", version)
        checks.append(_audio_check(code, ok, **kwargs))

    # --- narration coverage, ordering and timing ---------------------------
    missing: list[tuple[UUID, int, int]] = []
    drifting: list[tuple[UUID, int]] = []
    for segment_id, start, end in narration_intervals:
        span = max(end - start, 1)
        coverage = _covered(audible, start, end)
        if coverage * 2 < span:
            missing.append((segment_id, start, end))
        elif span - coverage > configuration.narration_timing_tolerance_us:
            drifting.append((segment_id, span - coverage))
    first_missing = missing[0] if missing else None
    add(
        FinalIssueCode.NARRATION_INTERVAL_MISSING,
        not missing,
        measurement=float(len(missing)),
        threshold=0.0,
        start_us=first_missing[1] if first_missing else None,
        end_us=first_missing[2] if first_missing else None,
        narration_segment_id=first_missing[0] if first_missing else None,
        message="every required narration interval must be audible in the final mix",
    )
    ordered = [start for _, start, _ in narration_intervals]
    add(
        FinalIssueCode.NARRATION_ORDER_MISMATCH,
        ordered == sorted(ordered),
        message="narration order must match the approved script projection",
    )
    identifiers = [segment_id for segment_id, _, _ in narration_intervals]
    duplicates = sorted({str(item) for item in identifiers if identifiers.count(item) > 1})
    add(
        FinalIssueCode.NARRATION_SEGMENT_DUPLICATED,
        not duplicates,
        measurement=float(len(duplicates)),
        message="no narration segment may appear twice: " + ", ".join(duplicates[:8]),
    )
    add(
        FinalIssueCode.NARRATION_SEGMENT_OMITTED,
        bool(narration_intervals),
        measurement=float(len(narration_intervals)),
        message="the approved narration projection must contain at least one segment",
    )
    worst_drift = max((amount for _, amount in drifting), default=0)
    add(
        FinalIssueCode.NARRATION_TIMING_DRIFT,
        not drifting,
        measurement=float(worst_drift),
        threshold=float(configuration.narration_timing_tolerance_us),
        unit="us",
        narration_segment_id=drifting[0][0] if drifting else None,
        message="narration timing must stay aligned with the T12 word timings",
    )

    # --- drift between the audio and visual timelines ----------------------
    if measurements.audio_duration_us is not None and measurements.video_duration_us is not None:
        drift = abs(measurements.audio_duration_us - measurements.video_duration_us)
        add(
            FinalIssueCode.AUDIO_VIDEO_DRIFT,
            drift <= configuration.av_drift_tolerance_us,
            measurement=float(drift),
            threshold=float(configuration.av_drift_tolerance_us),
            unit="us",
            message="the audio must not drift from the visual timeline",
        )

    # --- delivery loudness -------------------------------------------------
    integrated = loudness.get("integrated_lufs")
    if integrated is not None:
        deviation = abs(integrated - configuration.target_integrated_lufs)
        add(
            FinalIssueCode.LOUDNESS_OUT_OF_RANGE,
            deviation <= configuration.loudness_tolerance_lu,
            measurement=integrated,
            threshold=configuration.target_integrated_lufs,
            unit="LUFS",
            message=f"integrated loudness deviates by {deviation:.2f} LU",
        )
    true_peak = loudness.get("true_peak_dbtp")
    if true_peak is not None:
        add(
            FinalIssueCode.TRUE_PEAK_EXCEEDED,
            true_peak <= configuration.true_peak_ceiling_dbtp,
            measurement=true_peak,
            threshold=configuration.true_peak_ceiling_dbtp,
            unit="dBTP",
            message="true peak must remain below the configured ceiling",
        )
    ratio = clipping_ratio(statistics)
    add(
        FinalIssueCode.AUDIO_CLIPPING,
        ratio <= configuration.max_clipping_ratio,
        measurement=ratio,
        threshold=configuration.max_clipping_ratio,
        unit="ratio",
        message="the final mix must not clip",
    )
    add(
        FinalIssueCode.NON_FINITE_SAMPLE,
        all(math.isfinite(value) for value in statistics.values()),
        message="no non-finite audio sample measurement may be accepted",
    )

    # --- silence -----------------------------------------------------------
    leading = leading_silence_us(measurements)
    add(
        FinalIssueCode.LEADING_SILENCE_OUT_OF_RANGE,
        leading <= configuration.max_leading_silence_us,
        measurement=float(leading),
        threshold=float(configuration.max_leading_silence_us),
        unit="us",
        start_us=0,
        end_us=leading,
        message="leading silence exceeds the configured delivery limit",
    )
    trailing = trailing_silence_us(measurements, duration)
    add(
        FinalIssueCode.TRAILING_SILENCE_OUT_OF_RANGE,
        trailing <= configuration.max_trailing_silence_us,
        measurement=float(trailing),
        threshold=float(configuration.max_trailing_silence_us),
        unit="us",
        start_us=max(duration - trailing, 0),
        end_us=duration,
        message="trailing silence exceeds the configured delivery limit",
    )
    internal = _abnormal_internal_silence(measurements, narration_intervals, configuration)
    add(
        FinalIssueCode.ABNORMAL_INTERNAL_SILENCE,
        internal is None,
        measurement=None if internal is None else float(internal[1] - internal[0]),
        threshold=float(configuration.max_internal_silence_us),
        unit="us",
        start_us=None if internal is None else internal[0],
        end_us=None if internal is None else internal[1],
        message="an abnormal silence interrupts required narration",
    )

    # --- mix composition ---------------------------------------------------
    add(
        FinalIssueCode.CHANNEL_LAYOUT_MISMATCH,
        measurements.channels == configuration.expected_channels,
        measurement=float(measurements.channels or 0),
        threshold=float(configuration.expected_channels),
        message="channel count must match the delivery profile",
    )
    add(
        FinalIssueCode.SAMPLE_RATE_MISMATCH,
        measurements.sample_rate_hz == configuration.expected_sample_rate_hz,
        measurement=float(measurements.sample_rate_hz or 0),
        threshold=float(configuration.expected_sample_rate_hz),
        unit="Hz",
        message="sample rate must match the delivery profile",
    )
    add(
        FinalIssueCode.AUDIO_DISCONTINUITY,
        measurements.audio_decoded and measurements.decode_error_count == 0,
        measurement=float(measurements.decode_error_count),
        threshold=0.0,
        message="audio transitions must not introduce clicks or unintended cuts",
    )
    if manifest is not None:
        checks.extend(_manifest_checks(manifest, inputs, configuration, identity, version))
    return checks, {
        **{key: float(value) for key, value in loudness.items()},
        "clipping_ratio": ratio,
        "leading_silence_us": float(leading),
        "trailing_silence_us": float(trailing),
    }


def _abnormal_internal_silence(
    measurements: FinalMediaMeasurements,
    narration_intervals: list[tuple[UUID, int, int]],
    configuration: FinalQAConfiguration,
) -> tuple[int, int] | None:
    """A long silence that falls inside a required narration interval."""
    for interval in measurements.silence_intervals:
        start, end = int(interval.get("start_us", 0)), int(interval.get("end_us", 0))
        if end - start <= configuration.max_internal_silence_us:
            continue
        for _, narration_start, narration_end in narration_intervals:
            if start < narration_end and end > narration_start:
                return start, end
    return None


def _manifest_checks(
    manifest: dict[str, Any],
    inputs: FinalQAInput,
    configuration: FinalQAConfiguration,
    identity: str,
    version: str,
) -> list[FinalAudioCheck]:
    """Checks that read the render manifest's declared audio composition."""
    entries = [item for item in manifest.get("audio_entries", []) if isinstance(item, dict)]
    narration = [item for item in entries if item.get("role") == "narration"]
    beds = [item for item in entries if item.get("role") in {"music", "sfx"}]
    checks: list[FinalAudioCheck] = []

    masking = [
        item
        for item in beds
        if not item.get("duck_under_narration")
        and -int(item.get("gain_millidb", 0)) < configuration.min_narration_headroom_db * 1000
    ]
    checks.append(
        _audio_check(
            FinalIssueCode.NARRATION_MASKED_BY_BED,
            not masking,
            identity=identity,
            measurement=float(len(masking)),
            threshold=configuration.min_narration_headroom_db,
            unit="dB",
            tool_version_string=version,
            message="music and effects must duck or sit below narration by the configured headroom",
        )
    )
    overrunning = [
        item
        for item in entries
        if int(item.get("start_us", 0)) + int(item.get("duration_us", 0))
        > inputs.timeline_duration_us + configuration.duration_tolerance_us
    ]
    checks.append(
        _audio_check(
            FinalIssueCode.BACKGROUND_AUDIO_OVERRUN,
            not overrunning,
            identity=identity,
            measurement=float(len(overrunning)),
            tool_version_string=version,
            message="configured background audio must end within the canonical timeline",
        )
    )
    approved = set(inputs.narration_asset_ids)
    unapproved = [
        item
        for item in narration
        if _asset_id(item.get("asset")) is not None and _asset_id(item.get("asset")) not in approved
    ]
    checks.append(
        _audio_check(
            FinalIssueCode.UNAPPROVED_PROVIDER_AUDIO,
            len(narration) == 1 and not unapproved,
            identity=identity,
            measurement=float(len(unapproved)),
            tool_version_string=version,
            message="the mix must contain exactly the approved T12 narration and nothing else",
        )
    )
    return checks


def _asset_id(value: object) -> UUID | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("asset_id")
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None
