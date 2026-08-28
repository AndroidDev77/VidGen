"""Deterministic T20 media checks.

Everything here runs *before* any paid visual-agent request, so a corrupt,
mis-sized, black, frozen or wrong-length asset is rejected without spending
money. Every measurement carries its threshold, outcome, evidence timestamp,
tool, diagnostic code and, for failures, a repair code.

FFmpeg and ffprobe are invoked through subprocess argument arrays only, never a
shell string, and the video is streamed: a single bounded low-resolution
luminance pass produces the per-frame series the checks share.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from services.qa.rubric import DETERMINISTIC_CHECK_VERSION, DeterministicThresholds
from vidgen.contracts.visual_qa import (
    VisualQADeterministicMetric,
    VisualQADeterministicReport,
    VisualQARepairCode,
    VisualQATargetType,
)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
#: The luma pass is deliberately tiny: the checks need per-frame statistics, not
#: pixels, and a 64x36 gray plane keeps one clip's pass bounded and fast.
LUMA_WIDTH = 64
LUMA_HEIGHT = 36
#: Frames whose mean absolute inter-frame luma difference is under this read as
#: duplicates, which is how freeze and duplication ratios are measured.
DUPLICATE_YDIF_EPSILON = 0.35

_METADATA = re.compile(r"lavfi\.signalstats\.(?P<key>[A-Z]+)=(?P<value>-?[0-9.eE+]+)")
_FRAME = re.compile(r"^frame:\s*(?P<index>\d+)\s+pts:\s*(?P<pts>-?\d+)\s+pts_time:(?P<time>\S+)")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(binary: str) -> str:
    completed = subprocess.run([binary, "-version"], capture_output=True, text=True, check=False)
    if completed.returncode or not completed.stdout:
        return "unknown"
    return completed.stdout.splitlines()[0][:128]


@dataclass(frozen=True, slots=True)
class LumaFrame:
    index: int
    timestamp_us: int
    average: float
    minimum: float
    maximum: float
    difference: float


@dataclass(frozen=True, slots=True)
class MotionSeries:
    """Per-frame motion evidence shared by the checks and the sampler."""

    frames: tuple[LumaFrame, ...]
    high_motion_timestamps_us: tuple[int, ...]
    low_motion_timestamps_us: tuple[int, ...]

    @property
    def duplicate_ratio(self) -> float:
        moving = self.frames[1:]
        if not moving:
            return 0.0
        duplicates = sum(1 for frame in moving if frame.difference <= DUPLICATE_YDIF_EPSILON)
        return duplicates / len(moving)

    @property
    def mean_difference(self) -> float:
        moving = self.frames[1:]
        if not moving:
            return 0.0
        return sum(frame.difference for frame in moving) / len(moving)


def frame_interval_us(frame_rate: str) -> int:
    """Return one frame's duration in microseconds from an ffprobe rate string."""
    try:
        numerator, denominator = frame_rate.split("/", maxsplit=1)
        rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0
    if not math.isfinite(rate) or rate <= 0:
        return 0
    return round(1_000_000 / rate)


@dataclass(slots=True)
class MediaMeasurement:
    """The measured technical facts of one asset."""

    decodable: bool
    width: int | None = None
    height: int | None = None
    duration_us: int | None = None
    frame_rate: str = ""
    video_stream_count: int = 0
    audio_stream_count: int = 0
    container: str = ""
    codec: str = ""
    pixel_format: str = ""
    frame_count: int | None = None
    motion: MotionSeries | None = None
    black_frame_count: int = 0
    black_ratio: float = 0.0
    negative_timestamps: bool = False
    reversed_timestamps: bool = False
    diagnostics: list[str] = field(default_factory=list)


def _metric(
    code: str,
    *,
    outcome: str,
    diagnostic_code: str,
    tool: str,
    tool_version_text: str,
    measurement: float | None = None,
    threshold: float | None = None,
    repair_code: VisualQARepairCode | None = None,
    message: str = "",
    evidence_timestamp_us: int | None = None,
) -> VisualQADeterministicMetric:
    return VisualQADeterministicMetric(
        code=code,
        measurement=measurement,
        threshold=threshold,
        outcome=outcome,  # type: ignore[arg-type]
        evidence_timestamp_us=evidence_timestamp_us,
        tool=tool,
        tool_version=tool_version_text,
        diagnostic_code=diagnostic_code,
        repair_code=repair_code,
        message=message[:500],
    )


def probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise ValueError(f"ffprobe_failed: {completed.stderr.strip()[:200]}")
    import json

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("ffprobe_failed: malformed probe output") from error
    if not isinstance(payload, dict):
        raise ValueError("ffprobe_failed: unexpected probe payload")
    return payload


def full_decode(path: Path) -> tuple[bool, str]:
    """Decode every packet, proving the whole file is readable."""
    completed = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0, completed.stderr.strip()[:500]


def boundary_decode(path: Path, duration_us: int) -> bool:
    for position in (0, max(0, duration_us - 20_000)):
        completed = subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-nostdin",
                "-ss",
                f"{position / 1_000_000:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            return False
    return True


def luma_series(path: Path, *, sample_limit: int = 4096) -> MotionSeries:
    """Stream one bounded low-resolution luminance pass over the whole clip."""
    completed = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"scale={LUMA_WIDTH}:{LUMA_HEIGHT},format=gray,signalstats,metadata=mode=print:file=-",
            "-an",
            "-threads",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    frames: list[LumaFrame] = []
    index = 0
    timestamp_us = 0
    values: dict[str, float] = {}

    def flush() -> None:
        if not values:
            return
        frames.append(
            LumaFrame(
                index=index,
                timestamp_us=timestamp_us,
                average=values.get("YAVG", 0.0),
                minimum=values.get("YMIN", 0.0),
                maximum=values.get("YMAX", 0.0),
                difference=values.get("YDIF", 0.0),
            )
        )

    for line in completed.stdout.splitlines():
        header = _FRAME.match(line.strip())
        if header is not None:
            flush()
            values = {}
            index = int(header.group("index"))
            raw_time = header.group("time")
            try:
                timestamp_us = max(0, round(float(raw_time) * 1_000_000))
            except ValueError:
                timestamp_us = 0
            if len(frames) >= sample_limit:
                break
            continue
        match = _METADATA.search(line)
        if match is not None:
            try:
                values[match.group("key")] = float(match.group("value"))
            except ValueError:
                continue
    flush()
    ordered = tuple(frames)
    moving = ordered[1:]
    ranked = sorted(moving, key=lambda frame: (-frame.difference, frame.timestamp_us))
    quiet = sorted(moving, key=lambda frame: (frame.difference, frame.timestamp_us))
    return MotionSeries(
        frames=ordered,
        high_motion_timestamps_us=tuple(frame.timestamp_us for frame in ranked[:2]),
        low_motion_timestamps_us=tuple(frame.timestamp_us for frame in quiet[:2]),
    )


def measure(path: Path, target_type: VisualQATargetType) -> MediaMeasurement:
    """Measure one asset; a decode failure short-circuits every other check."""
    measurement = MediaMeasurement(decodable=False)
    try:
        payload = probe(path)
    except ValueError as error:
        measurement.diagnostics.append(str(error))
        return measurement
    streams = payload.get("streams", [])
    fmt = payload.get("format", {})
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        measurement.diagnostics.append("ffprobe_failed: unexpected stream layout")
        return measurement
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    measurement.video_stream_count = len(videos)
    measurement.audio_stream_count = len(audios)
    measurement.container = str(fmt.get("format_name", ""))
    if not videos:
        measurement.diagnostics.append("missing_video_stream")
        return measurement
    video = videos[0]
    try:
        measurement.width = int(video["width"])
        measurement.height = int(video["height"])
    except (KeyError, TypeError, ValueError):
        measurement.diagnostics.append("non_finite_geometry")
        return measurement
    measurement.codec = str(video.get("codec_name", ""))
    measurement.pixel_format = str(video.get("pix_fmt", ""))
    measurement.frame_rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    raw_duration = video.get("duration") or fmt.get("duration")
    if target_type is VisualQATargetType.VIDEO:
        try:
            seconds = float(raw_duration)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            measurement.diagnostics.append("non_finite_duration")
            return measurement
        if not math.isfinite(seconds) or seconds <= 0:
            measurement.diagnostics.append("non_finite_duration")
            return measurement
        measurement.duration_us = round(seconds * 1_000_000)
    decodable, stderr = full_decode(path)
    if not decodable:
        measurement.diagnostics.append(f"decode_failed: {stderr}")
        return measurement
    measurement.decodable = True
    if target_type is VisualQATargetType.KEYFRAME:
        return measurement
    if measurement.duration_us is not None and not boundary_decode(path, measurement.duration_us):
        measurement.decodable = False
        measurement.diagnostics.append("boundary_decode_failed")
        return measurement
    series = luma_series(path)
    measurement.motion = series
    measurement.frame_count = len(series.frames) or None
    timestamps = [frame.timestamp_us for frame in series.frames]
    measurement.negative_timestamps = any(value < 0 for value in timestamps)
    measurement.reversed_timestamps = timestamps != sorted(timestamps)
    return measurement


def evaluate(
    measurement: MediaMeasurement,
    *,
    target_type: VisualQATargetType,
    expected_width: int | None,
    expected_height: int | None,
    expected_duration_us: int | None,
    expects_stillness: bool,
    thresholds: DeterministicThresholds,
    ffmpeg_version: str,
    ffprobe_version: str,
) -> VisualQADeterministicReport:
    """Turn one measurement into the persisted, thresholded deterministic report."""
    metrics: list[VisualQADeterministicMetric] = []

    def add(**kwargs: object) -> None:
        metrics.append(_metric(**kwargs))  # type: ignore[arg-type]

    if not measurement.decodable:
        add(
            code="complete_decode",
            outcome="hard_failure",
            diagnostic_code=(measurement.diagnostics or ["decode_failed"])[0][:64],
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            repair_code=VisualQARepairCode.DECODE_FAILURE,
            message="; ".join(measurement.diagnostics) or "asset is not fully decodable",
        )
        return VisualQADeterministicReport(
            check_version=DETERMINISTIC_CHECK_VERSION,
            target_type=target_type,
            usable=False,
            metrics=metrics,
        )
    add(
        code="complete_decode",
        outcome="pass",
        diagnostic_code="decode_ok",
        tool=FFMPEG,
        tool_version_text=ffmpeg_version,
        message="every packet decoded",
    )
    if measurement.video_stream_count != 1:
        add(
            code="stream_layout",
            outcome="hard_failure",
            diagnostic_code="unexpected_stream_layout",
            tool=FFPROBE,
            tool_version_text=ffprobe_version,
            measurement=float(measurement.video_stream_count),
            threshold=1.0,
            repair_code=VisualQARepairCode.DECODE_FAILURE,
            message="exactly one video stream is required",
        )
    else:
        add(
            code="stream_layout",
            outcome="pass",
            diagnostic_code="stream_layout_ok",
            tool=FFPROBE,
            tool_version_text=ffprobe_version,
            measurement=1.0,
            threshold=1.0,
        )
    if expected_width and expected_height:
        matches = (measurement.width, measurement.height) == (expected_width, expected_height)
        add(
            code="canonical_geometry",
            outcome="pass" if matches else "hard_failure",
            diagnostic_code="geometry_ok" if matches else "incorrect_dimensions",
            tool=FFPROBE,
            tool_version_text=ffprobe_version,
            measurement=float(measurement.width or 0),
            threshold=float(expected_width),
            repair_code=None if matches else VisualQARepairCode.DECODE_FAILURE,
            message=""
            if matches
            else f"expected {expected_width}x{expected_height}, "
            f"measured {measurement.width}x{measurement.height}",
        )
    if target_type is VisualQATargetType.KEYFRAME:
        return VisualQADeterministicReport(
            check_version=DETERMINISTIC_CHECK_VERSION,
            target_type=target_type,
            usable=not any(item.outcome == "hard_failure" for item in metrics),
            measured_duration_us=None,
            width=measurement.width,
            height=measurement.height,
            frame_rate=measurement.frame_rate,
            metrics=metrics,
        )
    rate_ok = bool(measurement.frame_rate) and measurement.frame_rate not in {"0/0", "N/A"}
    add(
        code="frame_rate",
        outcome="pass" if rate_ok else "hard_failure",
        diagnostic_code="frame_rate_ok" if rate_ok else "non_finite_frame_rate",
        tool=FFPROBE,
        tool_version_text=ffprobe_version,
        repair_code=None if rate_ok else VisualQARepairCode.DECODE_FAILURE,
        message="" if rate_ok else f"unsupported frame rate {measurement.frame_rate!r}",
    )
    duration = measurement.duration_us
    if duration is None:
        add(
            code="duration_finite",
            outcome="hard_failure",
            diagnostic_code="non_finite_duration",
            tool=FFPROBE,
            tool_version_text=ffprobe_version,
            repair_code=VisualQARepairCode.DURATION_MISMATCH,
            message="measured duration is not finite",
        )
    elif expected_duration_us is not None:
        drift = abs(duration - expected_duration_us)
        if drift > thresholds.duration_hard_failure_us:
            outcome, repair = "hard_failure", VisualQARepairCode.DURATION_MISMATCH
            message = (
                f"duration drift {drift}us exceeds the "
                f"{thresholds.duration_hard_failure_us}us hard-failure threshold"
            )
        elif drift > thresholds.duration_warning_us:
            outcome, repair = "warning", VisualQARepairCode.DURATION_MISMATCH
            message = "duration drift is approaching the hard-failure threshold"
        else:
            outcome, repair, message = "pass", None, ""
        add(
            code="duration_matches_t13",
            outcome=outcome,
            diagnostic_code="duration_drift",
            tool=FFPROBE,
            tool_version_text=ffprobe_version,
            measurement=float(drift),
            threshold=float(thresholds.duration_hard_failure_us),
            repair_code=repair,
            message=message,
        )
    if measurement.negative_timestamps or measurement.reversed_timestamps:
        add(
            code="timestamp_monotonicity",
            outcome="hard_failure",
            diagnostic_code="timestamp_reversal"
            if measurement.reversed_timestamps
            else "negative_timestamp",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            repair_code=VisualQARepairCode.DECODE_FAILURE,
            message="decoded presentation timestamps are negative or non-monotonic",
        )
    else:
        add(
            code="timestamp_monotonicity",
            outcome="pass",
            diagnostic_code="timestamps_ok",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
        )
    series = measurement.motion
    if series is not None and series.frames:
        black = [frame for frame in series.frames if frame.average <= thresholds.black_luma_ceiling]
        ratio = len(black) / len(series.frames)
        if ratio >= thresholds.black_video_ratio:
            add(
                code="black_frames",
                outcome="hard_failure",
                diagnostic_code="black_video",
                tool=FFMPEG,
                tool_version_text=ffmpeg_version,
                measurement=ratio,
                threshold=thresholds.black_video_ratio,
                repair_code=VisualQARepairCode.BLACK_VIDEO,
                evidence_timestamp_us=black[0].timestamp_us,
                message="the clip is black or effectively empty",
            )
        elif len(black) > thresholds.max_black_frames:
            add(
                code="black_frames",
                outcome="warning",
                diagnostic_code="excessive_black_frames",
                tool=FFMPEG,
                tool_version_text=ffmpeg_version,
                measurement=float(len(black)),
                threshold=float(thresholds.max_black_frames),
                repair_code=VisualQARepairCode.BLACK_VIDEO,
                evidence_timestamp_us=black[0].timestamp_us,
                message=f"{len(black)} black frames detected",
            )
        else:
            add(
                code="black_frames",
                outcome="pass",
                diagnostic_code="black_frames_ok",
                tool=FFMPEG,
                tool_version_text=ffmpeg_version,
                measurement=float(len(black)),
                threshold=float(thresholds.max_black_frames),
            )
        freeze = series.duplicate_ratio
        freeze_exceeded = freeze > thresholds.freeze_ratio_warning
        add(
            code="freeze_ratio",
            outcome="warning" if freeze_exceeded and not expects_stillness else "pass",
            diagnostic_code="excessive_freeze"
            if freeze_exceeded and not expects_stillness
            else "freeze_ratio_ok",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            measurement=freeze,
            threshold=thresholds.freeze_ratio_warning,
            repair_code=VisualQARepairCode.EXCESSIVE_FREEZE
            if freeze_exceeded and not expects_stillness
            else None,
            evidence_timestamp_us=series.low_motion_timestamps_us[0]
            if series.low_motion_timestamps_us
            else None,
            message="the storyboard expects stillness for this shot"
            if freeze_exceeded and expects_stillness
            else "",
        )
        duplicated = freeze > thresholds.duplicate_frame_ratio_warning
        add(
            code="duplicate_frames",
            outcome="warning" if duplicated and not expects_stillness else "pass",
            diagnostic_code="excessive_duplicate_frames"
            if duplicated and not expects_stillness
            else "duplicate_frames_ok",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            measurement=freeze,
            threshold=thresholds.duplicate_frame_ratio_warning,
            repair_code=VisualQARepairCode.EXCESSIVE_FREEZE
            if duplicated and not expects_stillness
            else None,
        )
        flicker = series.mean_difference
        add(
            code="flicker",
            outcome="warning" if flicker > thresholds.flicker_delta_warning else "pass",
            diagnostic_code="excessive_flicker"
            if flicker > thresholds.flicker_delta_warning
            else "flicker_ok",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            measurement=flicker,
            threshold=thresholds.flicker_delta_warning,
            repair_code=VisualQARepairCode.EXCESSIVE_FLICKER
            if flicker > thresholds.flicker_delta_warning
            else None,
            evidence_timestamp_us=series.high_motion_timestamps_us[0]
            if series.high_motion_timestamps_us
            else None,
        )
        brightness = sum(frame.average for frame in series.frames) / len(series.frames)
        anomalous = (
            brightness < thresholds.brightness_floor or brightness > thresholds.brightness_ceiling
        )
        add(
            code="exposure",
            outcome="warning" if anomalous else "pass",
            diagnostic_code="exposure_anomaly" if anomalous else "exposure_ok",
            tool=FFMPEG,
            tool_version_text=ffmpeg_version,
            measurement=brightness,
            threshold=thresholds.brightness_ceiling,
        )
        for value in (brightness, flicker, freeze):
            if not math.isfinite(value):
                add(
                    code="finite_measurements",
                    outcome="hard_failure",
                    diagnostic_code="non_finite_measurement",
                    tool=FFMPEG,
                    tool_version_text=ffmpeg_version,
                    repair_code=VisualQARepairCode.DECODE_FAILURE,
                    message="a deterministic measurement is not finite",
                )
                break
    return VisualQADeterministicReport(
        check_version=DETERMINISTIC_CHECK_VERSION,
        target_type=target_type,
        usable=not any(item.outcome == "hard_failure" for item in metrics),
        measured_duration_us=measurement.duration_us,
        width=measurement.width,
        height=measurement.height,
        frame_rate=measurement.frame_rate,
        metrics=metrics,
    )


def warning_timestamps(report: VisualQADeterministicReport) -> tuple[int, ...]:
    """Timestamps the sampler must cover because a deterministic check flagged them."""
    return tuple(
        sorted(
            {
                metric.evidence_timestamp_us
                for metric in report.metrics
                if metric.outcome in {"warning", "hard_failure"}
                and metric.evidence_timestamp_us is not None
            }
        )
    )


def expects_stillness(camera_movement: str, subject_action: str) -> bool:
    """Whether T13 explicitly asks for a held pose, so freeze is intentional."""
    still_words = ("still", "motionless", "frozen", "held pose", "holds", "unmoving", "静")
    action = subject_action.lower()
    return camera_movement == "static" and any(word in action for word in still_words)


def merge(
    report: VisualQADeterministicReport, extra: Sequence[VisualQADeterministicMetric]
) -> VisualQADeterministicReport:
    """Append sample-derived metrics, recomputing usability from the union."""
    metrics = [*report.metrics, *extra]
    return report.model_copy(
        update={
            "metrics": metrics,
            "usable": not any(item.outcome == "hard_failure" for item in metrics),
        }
    )


# --- Frame-level deterministic analyzers -----------------------------------
#
# These run over the sampled frames rather than the whole stream. They are
# deliberately coarse, documented proxies built on Pillow, not trained
# detectors: each is exposed as a protocol so a deployment can configure a
# stronger representation without changing the pipeline or the thresholds. A
# proxy never invents certainty on its own - it produces a measurement, and the
# rubric decides what the measurement means.

#: Pixels brighter than this in the edge image count as a stroke.
TEXT_EDGE_THRESHOLD = 70
#: Mean edge/background transitions per row at which a band reads as full of
#: glyphs. Counting *transitions* rather than density is what separates text from
#: an object outline: a moving ellipse crosses a row twice, a line of type dozens
#: of times.
TEXT_TRANSITION_SATURATION = 14.0
#: Bands containing a row denser than this are solid structure, not glyphs.
TEXT_STRUCTURAL_ROW_CEILING = 0.6
TEXT_ANALYSIS_WIDTH = 640
#: Inclusive RGB envelope treated as skin for the coarse face-region proxy.
SKIN_RED = (95, 255)
SKIN_GREEN = (40, 220)
SKIN_BLUE = (20, 200)
STYLE_BINS = 4


@dataclass(frozen=True, slots=True)
class TextObservation:
    """One frame's unintended-text measurement and where it was found."""

    confidence: float
    band_top: float
    band_height: float


@dataclass(frozen=True, slots=True)
class RegionObservation:
    """A coarse face-region proxy: normalized centroid and area of skin-like pixels."""

    present: bool
    centre_x: float
    centre_y: float
    area_ratio: float


def _image(content: bytes) -> object:
    from io import BytesIO

    from PIL import Image

    return Image.open(BytesIO(content)).convert("RGB")


def detect_text(content: bytes) -> TextObservation:
    """Measure how strongly a frame reads as containing rendered text."""
    from PIL import Image, ImageFilter

    image = _image(content)
    gray = image.convert("L")  # type: ignore[attr-defined]
    if gray.width > TEXT_ANALYSIS_WIDTH:
        height = max(1, round(gray.height * TEXT_ANALYSIS_WIDTH / gray.width))
        gray = gray.resize((TEXT_ANALYSIS_WIDTH, height), Image.Resampling.BILINEAR)
    edges = gray.filter(ImageFilter.FIND_EDGES).crop(
        (2, 2, max(3, gray.width - 2), max(3, gray.height - 2))
    )
    pixels = edges.load()
    width, height = edges.size
    if pixels is None or width < 4 or height < 4:
        return TextObservation(confidence=0.0, band_top=0.0, band_height=0.0)
    transitions: list[int] = []
    densities: list[float] = []
    for y in range(height):
        previous = False
        crossings = 0
        strokes = 0
        for x in range(width):
            current = pixels[x, y] > TEXT_EDGE_THRESHOLD
            strokes += current
            crossings += current != previous
            previous = current
        transitions.append(crossings)
        densities.append(strokes / width)
    band = max(3, height // 30)
    best = 0.0
    best_top = 0
    for top in range(max(1, height - band)):
        if max(densities[top : top + band]) > TEXT_STRUCTURAL_ROW_CEILING:
            # A solid structural edge - a border or a filled bar - not glyphs.
            continue
        crossings = sum(transitions[top : top + band]) / band
        if crossings > best:
            best, best_top = crossings, top
    return TextObservation(
        confidence=min(1.0, best / TEXT_TRANSITION_SATURATION),
        band_top=best_top / height,
        band_height=band / height,
    )


def detect_region(content: bytes) -> RegionObservation:
    """Locate the dominant skin-toned region used as the face-track proxy."""
    from PIL import Image

    image = _image(content).resize((64, 36), Image.Resampling.BILINEAR)  # type: ignore[attr-defined]
    total = 0
    sum_x = 0
    sum_y = 0
    for y in range(36):
        for x in range(64):
            red, green, blue = image.getpixel((x, y))
            if (
                SKIN_RED[0] <= red <= SKIN_RED[1]
                and SKIN_GREEN[0] <= green <= SKIN_GREEN[1]
                and SKIN_BLUE[0] <= blue <= SKIN_BLUE[1]
                and red > blue
                and red >= green
            ):
                total += 1
                sum_x += x
                sum_y += y
    if not total:
        return RegionObservation(present=False, centre_x=0.0, centre_y=0.0, area_ratio=0.0)
    return RegionObservation(
        present=True,
        centre_x=sum_x / total / 64,
        centre_y=sum_y / total / 36,
        area_ratio=total / (64 * 36),
    )


def face_track_continuity(observations: Sequence[RegionObservation]) -> float:
    """Return 1.0 for a stable tracked region and 0.0 when it disappears."""
    present = [item for item in observations if item.present]
    if not observations or not present:
        return 0.0
    if len(present) != len(observations):
        return 0.0
    if len(observations) == 1:
        return 1.0
    penalties = []
    for previous, current in itertools.pairwise(observations):
        displacement = math.hypot(
            current.centre_x - previous.centre_x, current.centre_y - previous.centre_y
        )
        largest = max(previous.area_ratio, current.area_ratio) or 1.0
        area_change = abs(current.area_ratio - previous.area_ratio) / largest
        penalties.append(min(1.0, displacement + area_change))
    return max(0.0, 1.0 - sum(penalties) / len(penalties))


def style_descriptor(content: bytes) -> tuple[float, ...]:
    """A deterministic colour/edge signature used for perceptual style distance."""
    from PIL import Image, ImageFilter

    image = _image(content).resize((32, 32), Image.Resampling.BILINEAR)  # type: ignore[attr-defined]
    histogram = [0.0] * (STYLE_BINS**3)
    step = 256 // STYLE_BINS
    for y in range(32):
        for x in range(32):
            red, green, blue = image.getpixel((x, y))
            index = (
                min(STYLE_BINS - 1, red // step) * STYLE_BINS**2
                + min(STYLE_BINS - 1, green // step) * STYLE_BINS
                + min(STYLE_BINS - 1, blue // step)
            )
            histogram[index] += 1
    total = sum(histogram) or 1.0
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    pixels = edges.load()
    density = (
        sum(1 for y in range(32) for x in range(32) if pixels[x, y] > 40) / 1024
        if pixels is not None
        else 0.0
    )
    return (*(value / total for value in histogram), density)


def style_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Normalized L1 distance in [0, 1] between two style descriptors."""
    if len(left) != len(right) or not left:
        return 1.0
    return min(1.0, sum(abs(a - b) for a, b in zip(left, right, strict=True)) / 2)
