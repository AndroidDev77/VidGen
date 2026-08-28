"""Deterministic T20 frame sampling.

The sampler is pure integer/rational arithmetic over microseconds: the same
asset, the same T13 shot and the same sampling configuration always produce the
same requested timestamps, in the same canonical order, with the same selection
reasons. Nothing here is random.

Media is streamed through bounded temporary storage: one frame at a time is
decoded to a temporary file, hashed, handed to the caller and deleted. The
complete video is never loaded into memory.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from services.qa.deterministic import MotionSeries, sha256_bytes
from services.qa.rubric import SamplingConfiguration
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.contracts.visual_qa import VisualQASampleType

#: Selection priority when the configured sample budget is exceeded. Boundary and
#: action evidence outranks generic coverage, so a bounded package still proves
#: the dimensions that can hard-fail a shot.
SAMPLE_PRIORITY: tuple[VisualQASampleType, ...] = (
    VisualQASampleType.KEYFRAME_IMAGE,
    VisualQASampleType.FIRST_FRAME,
    VisualQASampleType.LAST_FRAME,
    VisualQASampleType.ACTION_WINDOW,
    VisualQASampleType.DETERMINISTIC_WARNING,
    VisualQASampleType.ACTION_BOUNDARY,
    VisualQASampleType.MIDPOINT,
    VisualQASampleType.CLAUSE_BOUNDARY,
    VisualQASampleType.CAMERA_CHANGE,
    VisualQASampleType.TRANSITION_BOUNDARY,
    VisualQASampleType.HIGH_MOTION,
    VisualQASampleType.LOW_MOTION,
    VisualQASampleType.FACE_TRACK,
    VisualQASampleType.OCR,
    VisualQASampleType.COVERAGE,
)


class SamplingError(RuntimeError):
    """Frame extraction failed in a way that makes the asset unusable."""


@dataclass(frozen=True, slots=True)
class PlannedSample:
    """One requested timestamp with the exact reason it was selected."""

    requested_timestamp_us: int
    sample_type: VisualQASampleType
    reason: str


@dataclass(frozen=True, slots=True)
class DecodedSample:
    """One decoded frame: requested and actual timestamp, bytes and digest."""

    planned: PlannedSample
    actual_timestamp_us: int
    content: bytes
    sha256: str
    width: int
    height: int


def _clamp(value: int, duration_us: int) -> int:
    return max(0, min(int(value), max(0, duration_us)))


def _split(duration_us: int, index: int, parts: int) -> int:
    """Exact rational split of a duration, rounded half-down to an integer."""
    return int(Fraction(duration_us * index, parts))


def action_window(shot: StoryboardShot, duration_us: int) -> tuple[int, int]:
    """Return the interval the T13 mandatory action is expected to occupy.

    The Director may persist an explicit window in shot provenance. Without one,
    the deterministic default is the central half of the usable duration, which
    is where a single mandatory beat is staged by the storyboard contract.
    """
    raw = shot.provenance.get("action_window_us")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            start, end = int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            start, end = -1, -1
        if 0 <= start < end:
            return _clamp(start, duration_us), _clamp(end, duration_us)
    return _split(duration_us, 1, 4), _split(duration_us, 3, 4)


def plan_keyframe_samples() -> list[PlannedSample]:
    """A keyframe is one still image; its only sample is the image itself."""
    return [
        PlannedSample(
            requested_timestamp_us=0,
            sample_type=VisualQASampleType.KEYFRAME_IMAGE,
            reason="selected T14 keyframe image",
        )
    ]


def plan_video_samples(
    shot: StoryboardShot,
    *,
    measured_duration_us: int,
    configuration: SamplingConfiguration,
    motion: MotionSeries | None = None,
    frame_interval_us: int = 0,
    warning_timestamps_us: Sequence[int] = (),
    requires_face_evidence: bool = True,
    requires_ocr_evidence: bool = True,
) -> list[PlannedSample]:
    """Return the canonical ordered sample plan for one canonical clip."""
    if measured_duration_us <= 0:
        raise SamplingError("cannot sample a clip without a measured positive duration")
    duration = measured_duration_us
    # The final decodable frame is never the exact duration: seeking past the last
    # presentation timestamp decodes nothing. Back off by the configured margin or
    # two measured frame intervals, whichever is larger, so the last sample always
    # lands on a real frame at any frame rate.
    backoff = max(configuration.final_frame_backoff_us, 2 * max(0, frame_interval_us))
    last = _clamp(duration - backoff, duration)
    candidates: list[PlannedSample] = [
        PlannedSample(0, VisualQASampleType.FIRST_FRAME, "first decodable frame"),
        PlannedSample(last, VisualQASampleType.LAST_FRAME, "final decodable frame"),
        PlannedSample(_split(duration, 1, 2), VisualQASampleType.MIDPOINT, "clip midpoint"),
    ]
    for index in range(1, configuration.coverage_sample_count + 1):
        position = _split(duration, index, configuration.coverage_sample_count + 1)
        candidates.append(
            PlannedSample(
                _clamp(position, last),
                VisualQASampleType.COVERAGE,
                f"evenly spaced coverage frame {index}/{configuration.coverage_sample_count}",
            )
        )
    candidates.append(
        PlannedSample(
            0,
            VisualQASampleType.CLAUSE_BOUNDARY,
            f"T13 clause start: {shot.clause_label or 'shot start'}",
        )
    )
    candidates.append(PlannedSample(last, VisualQASampleType.CLAUSE_BOUNDARY, "T13 clause end"))
    if shot.camera.movement != "static":
        candidates.append(
            PlannedSample(
                0,
                VisualQASampleType.CAMERA_CHANGE,
                f"T13 camera movement start: {shot.camera.movement}",
            )
        )
        candidates.append(
            PlannedSample(
                last,
                VisualQASampleType.CAMERA_CHANGE,
                f"T13 camera movement end: {shot.camera.movement}",
            )
        )
    if shot.transition_in.duration_us:
        candidates.append(
            PlannedSample(
                _clamp(shot.transition_in.duration_us, last),
                VisualQASampleType.TRANSITION_BOUNDARY,
                f"end of incoming {shot.transition_in.kind} transition",
            )
        )
    if shot.transition_out.duration_us:
        candidates.append(
            PlannedSample(
                _clamp(duration - shot.transition_out.duration_us, last),
                VisualQASampleType.TRANSITION_BOUNDARY,
                f"start of outgoing {shot.transition_out.kind} transition",
            )
        )
    start, end = action_window(shot, duration)
    candidates.append(
        PlannedSample(
            _clamp(start, last), VisualQASampleType.ACTION_BOUNDARY, "required action window start"
        )
    )
    candidates.append(
        PlannedSample(
            _clamp(end, last), VisualQASampleType.ACTION_BOUNDARY, "required action window end"
        )
    )
    span = max(0, end - start)
    for index in range(1, configuration.action_window_samples + 1):
        offset = _split(span, index, configuration.action_window_samples + 1)
        candidates.append(
            PlannedSample(
                _clamp(start + offset, last),
                VisualQASampleType.ACTION_WINDOW,
                f"inside the required action window ({index}"
                f"/{configuration.action_window_samples})",
            )
        )
    if motion is not None:
        for timestamp in motion.high_motion_timestamps_us:
            candidates.append(
                PlannedSample(
                    _clamp(timestamp, last),
                    VisualQASampleType.HIGH_MOTION,
                    "highest measured inter-frame motion",
                )
            )
        for timestamp in motion.low_motion_timestamps_us:
            candidates.append(
                PlannedSample(
                    _clamp(timestamp, last),
                    VisualQASampleType.LOW_MOTION,
                    "lowest measured inter-frame motion (freeze candidate)",
                )
            )
    for timestamp in warning_timestamps_us:
        candidates.append(
            PlannedSample(
                _clamp(timestamp, last),
                VisualQASampleType.DETERMINISTIC_WARNING,
                "frame associated with a deterministic warning",
            )
        )
    if requires_face_evidence:
        for index in (1, 2, 3):
            candidates.append(
                PlannedSample(
                    _clamp(_split(duration, index, 4), last),
                    VisualQASampleType.FACE_TRACK,
                    f"face-track continuity checkpoint {index}/3",
                )
            )
    if requires_ocr_evidence:
        candidates.append(
            PlannedSample(
                _clamp(_split(duration, 1, 3), last),
                VisualQASampleType.OCR,
                "OCR verification frame",
            )
        )
    return finalize_plan(candidates, configuration)


def finalize_plan(
    candidates: Sequence[PlannedSample], configuration: SamplingConfiguration
) -> list[PlannedSample]:
    """Deduplicate by timestamp, apply the bounded budget, and order canonically."""
    priority = {kind: index for index, kind in enumerate(SAMPLE_PRIORITY)}
    ordered = sorted(
        enumerate(candidates),
        key=lambda item: (priority.get(item[1].sample_type, len(priority)), item[0]),
    )
    chosen: dict[int, PlannedSample] = {}
    for _, candidate in ordered:
        if candidate.requested_timestamp_us in chosen:
            continue
        if len(chosen) >= configuration.max_samples:
            break
        chosen[candidate.requested_timestamp_us] = candidate
    return [chosen[key] for key in sorted(chosen)]


#: ``showinfo`` reports the presentation timestamp of the frame FFmpeg actually
#: decoded, which is what the manifest records next to the requested timestamp.
_SHOWINFO_PTS = re.compile(r"pts_time:(?P<value>[0-9]+(?:\.[0-9]+)?)")


def _decoded_timestamp_us(stderr: str, fallback_us: int) -> int:
    match = _SHOWINFO_PTS.search(stderr)
    if match is None:
        return fallback_us
    try:
        return max(0, round(float(match.group("value")) * 1_000_000))
    except ValueError:
        return fallback_us


def decode_sample(source: Path, planned: PlannedSample, workspace: Path) -> DecodedSample:
    """Decode exactly one frame into bounded temporary storage and hash it."""
    from PIL import Image

    destination = workspace / f"sample-{planned.requested_timestamp_us}.png"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-y",
            "-ss",
            f"{planned.requested_timestamp_us / 1_000_000:.6f}",
            # ``-copyts`` keeps source-relative presentation timestamps after the
            # input seek, so ``showinfo`` reports where the frame really is.
            "-copyts",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-an",
            "-threads",
            "1",
            "-fflags",
            "+bitexact",
            "-vf",
            "showinfo",
            "-c:v",
            "png",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode or not destination.is_file() or not destination.stat().st_size:
        raise SamplingError(f"frame at {planned.requested_timestamp_us}us is not decodable")
    try:
        content = destination.read_bytes()
        with Image.open(destination) as image:
            width, height = image.size
    finally:
        destination.unlink(missing_ok=True)
    return DecodedSample(
        planned=planned,
        actual_timestamp_us=_decoded_timestamp_us(completed.stderr, planned.requested_timestamp_us),
        content=content,
        sha256=sha256_bytes(content),
        width=width,
        height=height,
    )


def decode_samples(source: Path, plan: Sequence[PlannedSample]) -> list[DecodedSample]:
    """Decode a whole plan one frame at a time, preserving chronological order."""
    decoded: list[DecodedSample] = []
    seen: set[int] = set()
    with TemporaryDirectory(prefix="vidgen-qa-samples-") as raw:
        workspace = Path(raw)
        for planned in plan:
            sample = decode_sample(source, planned, workspace)
            # Two requested timestamps can land on the same decoded frame; the
            # manifest requires unique actual timestamps, so the first reason wins.
            if sample.actual_timestamp_us in seen:
                continue
            seen.add(sample.actual_timestamp_us)
            decoded.append(sample)
    decoded.sort(key=lambda item: item.actual_timestamp_us)
    return decoded


def load_still(source: Path) -> DecodedSample:
    """Load a still keyframe as the single sample of a keyframe QA run."""
    from PIL import Image

    content = source.read_bytes()
    with Image.open(source) as image:
        width, height = image.size
    return DecodedSample(
        planned=plan_keyframe_samples()[0],
        actual_timestamp_us=0,
        content=content,
        sha256=sha256_bytes(content),
        width=width,
        height=height,
    )
