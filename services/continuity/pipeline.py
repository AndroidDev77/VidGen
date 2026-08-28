"""Restartable deterministic core of the T19 continuity pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from services.continuity.bindings import make_bundle
from services.continuity.invalidation import affected_shots
from vidgen.contracts.continuity import ReferenceBundleItem, ShotReferenceBundle

PIPELINE_VERSION = "continuity-reference/1.0"


@dataclass(frozen=True, slots=True)
class CanonicalShotReferences:
    shot_id: UUID
    sequence: int
    character_identity_version_ids: tuple[UUID, ...] = ()
    character_state_snapshot_ids: tuple[UUID, ...] = ()
    location_identity_version_id: UUID | None = None
    location_state_snapshot_id: UUID | None = None
    references: tuple[ReferenceBundleItem, ...] = ()
    required_props: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class ContinuityPipeline:
    """Idempotently materialize bundles; persistence/provider work stays injected."""

    completed: dict[str, ShotReferenceBundle] = field(default_factory=dict)

    def bind_shots(
        self,
        *,
        project_id: UUID,
        storyboard_run_id: UUID,
        shots: list[CanonicalShotReferences],
        provider_reference_limit: int,
    ) -> list[ShotReferenceBundle]:
        bundles: list[ShotReferenceBundle] = []
        for shot in sorted(shots, key=lambda item: (item.sequence, str(item.shot_id))):
            bundle = make_bundle(
                project_id=project_id,
                storyboard_run_id=storyboard_run_id,
                shot_id=shot.shot_id,
                shot_sequence=shot.sequence,
                references=list(shot.references),
                provider_reference_limit=provider_reference_limit,
                character_identity_version_ids=list(shot.character_identity_version_ids),
                character_state_snapshot_ids=list(shot.character_state_snapshot_ids),
                location_identity_version_id=shot.location_identity_version_id,
                location_state_snapshot_id=shot.location_state_snapshot_id,
                required_props=list(shot.required_props),
                warnings=list(shot.warnings),
            )
            bundles.append(self.completed.setdefault(bundle.bundle_hash, bundle))
        return bundles

    def invalidation(
        self, dependencies: dict[UUID, set[UUID]], changed_entities: set[UUID]
    ) -> list[UUID]:
        return affected_shots(dependencies, changed_entities)
