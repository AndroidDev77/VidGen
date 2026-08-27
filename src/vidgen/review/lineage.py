"""Lineage identity for render approval.

An approval binds the render to the exact selected script, narration,
storyboard, shot outputs and caption identity it was produced from. When any of
those change the recomputed hash differs, so the stored approval remains
historical but no longer applies to the new lineage.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID


def render_lineage_hash(
    *,
    project_id: UUID,
    script_id: UUID | None,
    script_version: int | None,
    narration_run_id: UUID | None,
    storyboard_run_id: UUID | None,
    render_identity: str | None,
    caption_identity: str | None,
    selected_video_asset_ids: list[UUID],
) -> str:
    material = {
        "project_id": str(project_id),
        "script_id": str(script_id) if script_id else None,
        "script_version": script_version,
        "narration_run_id": str(narration_run_id) if narration_run_id else None,
        "storyboard_run_id": str(storyboard_run_id) if storyboard_run_id else None,
        "render_identity": render_identity,
        "caption_identity": caption_identity,
        "selected_video_asset_ids": sorted(str(item) for item in selected_video_asset_ids),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
