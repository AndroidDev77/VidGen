"""Evidence assembly, deterministic frame sampling and the contact sheet.

Every actionable T22 finding must point at something that really exists: an
exact global timestamp range, and where applicable a decoded frame, a caption
cue or an audio interval. This module owns that mapping, so no other stage can
invent a timestamp or cite a frame that was never sampled.

Sampling is deterministic by construction. The same render, the same
configuration and the same timeline always select the same timestamps, produce
the same frame IDs and lay out the same contact sheet, which is what makes a
resumed run free and a fixture reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid5

from vidgen.contracts.final_editorial import (
    FinalDeterministicCheck,
    FinalEditorialEvidence,
    FinalQAConfiguration,
    FinalQAInput,
)

#: Deterministic namespace so identical inputs always produce identical IDs.
FINAL_QA_NAMESPACE = UUID("8a5c1f27-3a1b-5b7d-9c0e-1f4a6d2b8e33")
CONTACT_SHEET_TILE_WIDTH = 320


def deterministic_id(*parts: object) -> UUID:
    return uuid5(FINAL_QA_NAMESPACE, ":".join(str(part) for part in parts))


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """One deterministically selected, decoded and hashed frame of the render."""

    sample_id: UUID
    sequence: int
    timestamp_us: int
    shot_id: UUID | None
    content: bytes
    sha256: str
    contact_sheet_position: int | None = None
    media_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class ContactSheet:
    """A rendered contact sheet plus its exact tile-to-sample mapping."""

    content: bytes
    columns: int
    rows: int
    positions: dict[UUID, int]
    sha256: str
    media_type: str = "image/png"


def plan_sample_timestamps(inputs: FinalQAInput, configuration: FinalQAConfiguration) -> list[int]:
    """Choose sample timestamps: one per shot, then evenly spaced filler.

    Shot-anchored samples come first so continuity findings always have a frame
    inside the shot they accuse. Filler samples spread across the timeline so a
    defect between shot midpoints is still visible.
    """
    duration = inputs.timeline_duration_us
    chosen: list[int] = []
    for shot in inputs.shots:
        midpoint = shot.global_start_us + (shot.global_end_us - shot.global_start_us) // 2
        chosen.append(min(max(midpoint, 0), max(duration - 1, 0)))
    remaining = configuration.editorial_sample_count - len(chosen)
    if remaining > 0 and duration > 0:
        step = duration / (remaining + 1)
        chosen.extend(min(round(step * (index + 1)), duration - 1) for index in range(remaining))
    unique = sorted({timestamp for timestamp in chosen if timestamp >= 0})
    return unique[: configuration.editorial_sample_count]


def _shot_for(inputs: FinalQAInput, timestamp_us: int) -> UUID | None:
    for shot in inputs.shots:
        if shot.global_start_us <= timestamp_us < shot.global_end_us:
            return shot.shot_id
    return None


def extract_frames(
    path: Path,
    inputs: FinalQAInput,
    configuration: FinalQAConfiguration,
    *,
    timeout: int = 120,
) -> list[SampledFrame]:
    """Decode exactly one PNG frame per planned timestamp, via argument arrays."""
    import hashlib

    frames: list[SampledFrame] = []
    for sequence, timestamp_us in enumerate(plan_sample_timestamps(inputs, configuration)):
        content = _decode_png(path, timestamp_us, timeout=timeout)
        if content is None:
            continue
        frames.append(
            SampledFrame(
                sample_id=deterministic_id("sample", inputs.render_identity, timestamp_us),
                sequence=sequence,
                timestamp_us=timestamp_us,
                shot_id=_shot_for(inputs, timestamp_us),
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return frames


def _decode_png(path: Path, timestamp_us: int, *, timeout: int) -> bytes | None:
    import subprocess

    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{timestamp_us / 1_000_000:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    return completed.stdout if completed.returncode == 0 and completed.stdout else None


def build_contact_sheet(frames: Sequence[SampledFrame], *, columns: int) -> ContactSheet | None:
    """Render a deterministic contact sheet preserving order and tile position."""
    import hashlib

    from PIL import Image

    if not frames:
        return None
    tiles: list[Image.Image] = []
    for frame in frames:
        with Image.open(BytesIO(frame.content)) as image:
            picture = image.convert("RGB")
            height = max(1, round(picture.height * CONTACT_SHEET_TILE_WIDTH / picture.width))
            tiles.append(
                picture.resize((CONTACT_SHEET_TILE_WIDTH, height), Image.Resampling.BILINEAR)
            )
    tile_height = max(tile.height for tile in tiles)
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (CONTACT_SHEET_TILE_WIDTH * columns, tile_height * rows), (16, 16, 16))
    positions: dict[UUID, int] = {}
    for index, (frame, tile) in enumerate(zip(frames, tiles, strict=True)):
        column, row = index % columns, index // columns
        sheet.paste(tile, (column * CONTACT_SHEET_TILE_WIDTH, row * tile_height))
        positions[frame.sample_id] = index
    buffer = BytesIO()
    # No timestamp chunk and no optimization pass: the same frames must produce
    # the same bytes, because the sheet's hash is part of the QA evidence.
    sheet.save(buffer, format="PNG")
    content = buffer.getvalue()
    return ContactSheet(
        content=content,
        columns=columns,
        rows=rows,
        positions=positions,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def frame_evidence(
    frame: SampledFrame,
    *,
    contact_sheet_asset_id: UUID | None = None,
    frame_asset_id: UUID | None = None,
    explanation: str = "",
    code: str = "",
) -> FinalEditorialEvidence:
    """Evidence anchored to one exact sampled frame of the assembled render."""
    return FinalEditorialEvidence(
        evidence_id=deterministic_id("frame-evidence", frame.sample_id, code),
        evidence_type=(
            "contact_sheet_tile" if frame.contact_sheet_position is not None else "sampled_frame"
        ),
        start_us=frame.timestamp_us,
        end_us=frame.timestamp_us,
        frame_asset_id=frame_asset_id,
        sample_id=frame.sample_id,
        contact_sheet_asset_id=contact_sheet_asset_id,
        contact_sheet_position=frame.contact_sheet_position,
        shot_id=frame.shot_id,
        tool="ffmpeg",
        explanation=explanation[:500],
    )


def check_evidence(
    check: FinalDeterministicCheck, *, shot_id: UUID | None = None
) -> FinalEditorialEvidence:
    """Evidence for a deterministic measurement, located on the global timeline."""
    start = check.start_us or 0
    return FinalEditorialEvidence(
        evidence_id=deterministic_id("check-evidence", check.check_id),
        evidence_type=(
            "caption_cue"
            if getattr(check, "cue_sequence", None) is not None
            else "deterministic_measurement"
        ),
        start_us=start,
        end_us=check.end_us if check.end_us is not None else start,
        caption_cue_sequence=getattr(check, "cue_sequence", None),
        shot_id=shot_id,
        measurement=check.measurement,
        threshold=check.threshold,
        tool=check.tool,
        tool_version=check.tool_version,
        explanation=check.message[:500],
    )


def nearest_frame(frames: Sequence[SampledFrame], timestamp_us: int) -> SampledFrame | None:
    """The sampled frame a finding at this timestamp should cite."""
    if not frames:
        return None
    return min(frames, key=lambda frame: abs(frame.timestamp_us - timestamp_us))
