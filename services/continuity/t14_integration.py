"""Additive T14 compatibility adapter for approved T19 bundles."""

from __future__ import annotations

from vidgen.contracts.continuity import ShotReferenceBundle
from vidgen.contracts.image_generation import ImageReferenceBinding


def bundle_references(bundle: ShotReferenceBundle) -> list[ImageReferenceBinding]:
    """Translate a verified immutable bundle without changing legacy T14 behavior."""
    role = {
        "character_identity": "character",
        "character_state": "character",
        "location_identity": "location",
        "location_state": "location",
        "prop": "approved",
    }
    return [
        ImageReferenceBinding(
            asset_id=item.asset_id,
            sha256=item.sha256,
            semantic_role=role[item.role],  # type: ignore[arg-type]
            required=item.required,
            order=order,
            media_type="image/png",
        )
        for order, item in enumerate(bundle.references)
    ]


def continuity_prompt_identity(bundle: ShotReferenceBundle) -> dict[str, object]:
    return {
        "compatibility_mode": "continuity_references_v1",
        "reference_bundle_hash": bundle.bundle_hash,
        "character_identity_version_ids": [
            str(value) for value in bundle.character_identity_version_ids
        ],
        "character_state_snapshot_ids": [
            str(value) for value in bundle.character_state_snapshot_ids
        ],
        "location_identity_version_id": (
            str(bundle.location_identity_version_id)
            if bundle.location_identity_version_id
            else None
        ),
        "location_state_snapshot_id": (
            str(bundle.location_state_snapshot_id) if bundle.location_state_snapshot_id else None
        ),
        "ordered_reference_hashes": [item.sha256 for item in bundle.references],
    }
