from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from vidgen.contracts.media import ExtractedFrame


def contact_sheet_manifest(frames: Sequence[ExtractedFrame], *, columns: int = 4) -> bytes:
    """Create canonical contact-sheet metadata; image rendering remains an activity concern."""
    if columns < 1:
        raise ValueError("columns must be positive")
    payload = {
        "schema_version": "1.0",
        "columns": columns,
        "frames": [
            {
                "asset_id": str(frame.asset_id),
                "scene_sequence": frame.scene_sequence,
                "sha256": frame.sha256,
                "timestamp_seconds": frame.timestamp_seconds,
            }
            for frame in sorted(
                frames, key=lambda item: (item.scene_sequence, item.timestamp_seconds)
            )
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def contact_sheet_hash(frames: Sequence[ExtractedFrame], *, columns: int = 4) -> str:
    return hashlib.sha256(contact_sheet_manifest(frames, columns=columns)).hexdigest()
