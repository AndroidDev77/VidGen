"""Immutable shot bundle construction and provider-limit compaction."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from services.continuity.identity import canonical_hash
from services.continuity.state_resolver import RESOLVER_VERSION
from vidgen.contracts.continuity import ReferenceBundleItem, ShotReferenceBundle

BUNDLE_NAMESPACE = UUID("ae54616e-b07f-40cf-b4f5-7ea90c28bd61")
ROLE_ORDER = {
    "character_identity": 0,
    "character_state": 1,
    "location_identity": 2,
    "location_state": 3,
    "prop": 4,
}


def compact_references(
    references: list[ReferenceBundleItem], limit: int
) -> tuple[list[ReferenceBundleItem], list[str]]:
    ordered = sorted(
        references, key=lambda ref: (ref.priority, ROLE_ORDER[ref.role], str(ref.asset_id))
    )
    required = [ref for ref in ordered if ref.required]
    if len(required) > limit:
        raise ValueError(f"provider limit {limit} cannot fit {len(required)} required references")
    kept = required + [ref for ref in ordered if not ref.required][: limit - len(required)]
    kept_ids = {ref.asset_id for ref in kept}
    omitted = [
        f"{ref.asset_id}:provider_reference_limit"
        for ref in ordered
        if ref.asset_id not in kept_ids
    ]
    return sorted(
        kept, key=lambda ref: (ref.priority, ROLE_ORDER[ref.role], str(ref.asset_id))
    ), omitted


def make_bundle(
    *,
    project_id: UUID,
    storyboard_run_id: UUID,
    shot_id: UUID,
    shot_sequence: int,
    references: list[ReferenceBundleItem],
    provider_reference_limit: int,
    character_identity_version_ids: list[UUID] | None = None,
    character_state_snapshot_ids: list[UUID] | None = None,
    location_identity_version_id: UUID | None = None,
    location_state_snapshot_id: UUID | None = None,
    required_props: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ShotReferenceBundle:
    kept, omitted = compact_references(references, provider_reference_limit)
    identity = {
        "project_id": str(project_id),
        "storyboard_run_id": str(storyboard_run_id),
        "shot_id": str(shot_id),
        "shot_sequence": shot_sequence,
        "references": [r.model_dump(mode="json") for r in kept],
        "character_identity_version_ids": [str(v) for v in character_identity_version_ids or []],
        "character_state_snapshot_ids": [str(v) for v in character_state_snapshot_ids or []],
        "location_identity_version_id": str(location_identity_version_id)
        if location_identity_version_id
        else None,
        "location_state_snapshot_id": str(location_state_snapshot_id)
        if location_state_snapshot_id
        else None,
        "required_props": required_props or [],
        "resolver_version": RESOLVER_VERSION,
    }
    digest = canonical_hash(identity)
    return ShotReferenceBundle(
        id=uuid5(BUNDLE_NAMESPACE, digest),
        project_id=project_id,
        storyboard_run_id=storyboard_run_id,
        shot_id=shot_id,
        shot_sequence=shot_sequence,
        character_identity_version_ids=character_identity_version_ids or [],
        character_state_snapshot_ids=character_state_snapshot_ids or [],
        location_identity_version_id=location_identity_version_id,
        location_state_snapshot_id=location_state_snapshot_id,
        references=kept,
        required_props=required_props or [],
        continuity_warnings=warnings or [],
        omitted_references=omitted,
        provider_reference_limit=provider_reference_limit,
        bundle_hash=digest,
        resolver_version=RESOLVER_VERSION,
        created_at=datetime.now(UTC),
    )
