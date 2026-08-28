"""Evidence assembly and the contact-sheet mapping.

Every actionable finding must point at a frame that actually exists: an exact
sample, its source-relative and shot-relative timestamps, its SHA-256, and -
when a contact sheet was built - the tile it occupies. This module owns that
mapping so no other stage can fabricate a timestamp.

A contact sheet is a bounded convenience for the visual agent, never a
replacement for the individual frames: the manifest keeps a position for every
tile, so a finding cited against tile 4 resolves back to one exact sample ID and
timestamp.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID, uuid5

from vidgen.contracts.visual_qa import (
    VisualQABoundingBox,
    VisualQAEvidence,
    VisualQAEvidenceType,
    VisualQASample,
)

#: Deterministic namespace so identical inputs always produce identical IDs.
EVIDENCE_NAMESPACE = UUID("6f2b1d84-0d6f-5f8a-9a1e-2c0d7d0a3f11")
CONTACT_SHEET_TILE_WIDTH = 320


def deterministic_id(*parts: object) -> UUID:
    return uuid5(EVIDENCE_NAMESPACE, ":".join(str(part) for part in parts))


@dataclass(frozen=True, slots=True)
class ContactSheet:
    """A rendered contact sheet plus its exact tile-to-sample mapping."""

    content: bytes
    columns: int
    rows: int
    positions: dict[UUID, int]
    media_type: str = "image/png"


def build_contact_sheet(
    samples: Sequence[tuple[VisualQASample, bytes]], *, columns: int
) -> ContactSheet | None:
    """Render a deterministic contact sheet preserving sample order and position."""
    from PIL import Image

    if not samples:
        return None
    tiles: list[Image.Image] = []
    for _, content in samples:
        with Image.open(BytesIO(content)) as image:
            frame = image.convert("RGB")
            height = max(1, round(frame.height * CONTACT_SHEET_TILE_WIDTH / frame.width))
            tiles.append(
                frame.resize((CONTACT_SHEET_TILE_WIDTH, height), Image.Resampling.BILINEAR)
            )
    tile_height = max(tile.height for tile in tiles)
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (CONTACT_SHEET_TILE_WIDTH * columns, tile_height * rows), (16, 16, 16))
    positions: dict[UUID, int] = {}
    for index, ((sample, _), tile) in enumerate(zip(samples, tiles, strict=True)):
        column, row = index % columns, index // columns
        sheet.paste(tile, (column * CONTACT_SHEET_TILE_WIDTH, row * tile_height))
        positions[sample.sample_id] = index
    buffer = BytesIO()
    # ``optimize`` off and no timestamp chunk: the same frames must yield the
    # same bytes, because the sheet's hash is part of the QA evidence.
    sheet.save(buffer, format="PNG")
    return ContactSheet(content=buffer.getvalue(), columns=columns, rows=rows, positions=positions)


def frame_evidence(
    sample: VisualQASample,
    *,
    explanation: str = "",
    confidence: float = 1.0,
    bounding_box: VisualQABoundingBox | None = None,
    compared_reference_asset_id: UUID | None = None,
    entity_kind: str = "none",
    entity_id: UUID | None = None,
    prop_reference: str | None = None,
    measurement: float | None = None,
    finding_code: str = "",
) -> VisualQAEvidence:
    """Build evidence anchored to one exact sampled frame."""
    evidence_type = (
        VisualQAEvidenceType.REFERENCE_COMPARISON
        if compared_reference_asset_id is not None
        else VisualQAEvidenceType.SAMPLE_FRAME
    )
    return VisualQAEvidence(
        evidence_id=deterministic_id("frame", sample.sample_id, finding_code, entity_id),
        evidence_type=evidence_type,
        sample_id=sample.sample_id,
        frame_asset_id=sample.frame_asset_id,
        source_asset_id=sample.source_asset_id,
        source_relative_timestamp_us=sample.actual_timestamp_us,
        shot_relative_timestamp_us=sample.shot_relative_timestamp_us,
        contact_sheet_position=sample.contact_sheet_position,
        bounding_box=bounding_box,
        entity_kind=entity_kind,  # type: ignore[arg-type]
        entity_id=entity_id,
        prop_reference=prop_reference,
        compared_reference_asset_id=compared_reference_asset_id,
        measurement=measurement,
        confidence=confidence,
        explanation=explanation[:500],
    )


def measurement_evidence(
    sample: VisualQASample | None,
    *,
    source_asset_id: UUID,
    code: str,
    measurement: float | None,
    explanation: str,
    timestamp_us: int | None = None,
) -> VisualQAEvidence:
    """Build evidence for a deterministic measurement, located when possible."""
    if sample is not None:
        return frame_evidence(
            sample,
            explanation=explanation,
            measurement=measurement,
            finding_code=code,
        ).model_copy(update={"evidence_type": VisualQAEvidenceType.DETERMINISTIC_MEASUREMENT})
    return VisualQAEvidence(
        evidence_id=deterministic_id("measurement", source_asset_id, code, timestamp_us),
        evidence_type=VisualQAEvidenceType.WHOLE_FILE,
        source_asset_id=source_asset_id,
        source_relative_timestamp_us=timestamp_us,
        shot_relative_timestamp_us=timestamp_us,
        measurement=measurement,
        confidence=1.0,
        explanation=explanation[:500],
    )


def nearest_sample(
    samples: Sequence[VisualQASample], timestamp_us: int | None
) -> VisualQASample | None:
    """Locate the sampled frame a deterministic measurement should cite."""
    if timestamp_us is None or not samples:
        return None
    return min(samples, key=lambda sample: abs(sample.actual_timestamp_us - timestamp_us))
