"""Production orchestration of the T19 continuity-reference lifecycle.

The deterministic T19 primitives already exist - candidate selection, identity
bibles, provider generation through the T14 boundary, validation, bundle
compaction and targeted regeneration. What was missing was the piece that
persists them in order and can be driven by a worker. That is this module, and
it deliberately adds no new pipeline: every step below calls an existing T19
component and writes to an existing T19 table.

Two behaviours are worth stating outright, because they are what makes T19 safe
to run inside the normal project lifecycle:

* **A project with nothing to reference completes deterministically.** An
  entity earns a reference sheet only when T09 actually persisted candidate
  frames for it. A project with no such evidence produces no drafts and
  therefore never waits for an approval that could not arrive.
* **Everything is keyed by identity, not by attempt.** An identity version, a
  reference sheet and a shot bundle are each addressed by a content hash, so a
  re-run of a build reuses what already exists instead of paying for it again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Table, insert, select, update
from sqlalchemy.orm import Session

from services.continuity.bible_builder import build_character_bible, build_location_bible
from services.continuity.identity import canonical_hash
from services.continuity.pipeline import CanonicalShotReferences, ContinuityPipeline
from services.continuity.reference_selector import select_candidates
from services.continuity.regeneration import ContinuityRegenerator
from vidgen.contracts.continuity import (
    CandidateScores,
    CharacterReferenceCandidate,
    EvidenceLink,
    ReferenceBundleItem,
    ReferenceGenerationRequest,
    ReferenceGenerationResult,
    ReferenceInvalidation,
)
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_candidates,
    character_reference_sets,
    location_identity_versions,
    location_reference_candidates,
    location_reference_sets,
    shot_reference_bindings,
)
from vidgen.db.models import Asset, Character, Location
from vidgen.db.storyboard_models import StoryboardShotRecord

EntityKind = Literal["character", "location"]
#: The provider reference limit T14 compacts a bundle down to. Kept here so the
#: binding stage and the API projection agree without importing a provider.
PROVIDER_REFERENCE_LIMIT = 4
ORCHESTRATOR_VERSION = "continuity-orchestrator/1.0"


class ContinuityOrchestrationError(RuntimeError):
    """A structured, actionable T19 orchestration failure."""

    def __init__(self, code: str, summary: str, *, retryable: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable


#: The narrow generator surface the orchestrator depends on. Both the free
#: deterministic fake and the provider-backed generator satisfy it, so the
#: orchestration is identical in tests and in production.
ReferenceSheetGenerator = Callable[
    [ReferenceGenerationRequest, list[str]], ReferenceGenerationResult
]


@dataclass(frozen=True, slots=True)
class EntityRequirement:
    """One entity that needs - or explicitly does not need - a reference sheet."""

    kind: EntityKind
    entity_id: UUID
    display_name: str
    definition: dict[str, Any]
    candidate_asset_ids: tuple[UUID, ...]
    candidate_hashes: tuple[str, ...]

    @property
    def required(self) -> bool:
        """A reference sheet needs evidence frames; without them there is none."""
        return bool(self.candidate_asset_ids)


@dataclass(frozen=True, slots=True)
class ReferenceBuildOutcome:
    reference_run_id: UUID
    draft_reference_set_ids: tuple[UUID, ...] = ()
    reused_reference_set_ids: tuple[UUID, ...] = ()
    requires_approval: bool = False
    entity_count: int = 0


@dataclass(slots=True)
class ReferenceApplyOutcome:
    project_id: UUID
    bound_shot_ids: tuple[UUID, ...] = ()
    invalidation: ReferenceInvalidation | None = None
    approved_reference_set_ids: tuple[UUID, ...] = ()
    regenerated_shot_ids: tuple[UUID, ...] = field(default_factory=tuple)


def _now() -> datetime:
    return datetime.now(UTC)


def _candidate_tables(kind: EntityKind) -> tuple[Table, Table, Table, str]:
    """Return the (identity, candidate, reference-set, entity-column) tables."""
    if kind == "character":
        return (
            character_identity_versions,
            character_reference_candidates,
            character_reference_sets,
            "character_id",
        )
    return (
        location_identity_versions,
        location_reference_candidates,
        location_reference_sets,
        "location_id",
    )


def _observations(definition: dict[str, Any]) -> dict[str, list[str]]:
    """Coerce a persisted entity definition into bible observations.

    Only scalar and list-of-scalar fields are considered: an identity bible is
    built from stated traits, and anything structured enough to need
    interpretation belongs to T10, not here.
    """
    observations: dict[str, list[str]] = {}
    for key, value in sorted(definition.items()):
        if isinstance(value, str) and value.strip():
            observations[key] = [value.strip()]
        elif isinstance(value, list):
            values = [str(item).strip() for item in value if isinstance(item, str | int | float)]
            if values:
                observations[key] = values
    return observations


def resolve_requirements(session: Session, project_id: UUID) -> list[EntityRequirement]:
    """Every character and location of a project, and whether it needs a sheet.

    Ordered by kind then canonical name so a rebuild is byte-for-byte stable.
    """
    requirements: list[EntityRequirement] = []
    for kind, model in (("character", Character), ("location", Location)):
        for entity in session.scalars(
            select(model)
            .where(model.project_id == project_id)
            .order_by(model.canonical_name, model.id)
        ):
            identities, candidates, _, entity_column = _candidate_tables(kind)  # type: ignore[arg-type]
            rows = list(
                session.execute(
                    select(candidates)
                    .join(identities, candidates.c.identity_version_id == identities.c.id)
                    .where(
                        identities.c.project_id == project_id,
                        identities.c[entity_column] == entity.id,
                    )
                ).mappings()
            )
            selected = _rank_candidates(session, rows)
            requirements.append(
                EntityRequirement(
                    kind=kind,  # type: ignore[arg-type]
                    entity_id=entity.id,
                    display_name=entity.canonical_name,
                    definition=dict(entity.definition or {}),
                    candidate_asset_ids=tuple(asset_id for asset_id, _ in selected),
                    candidate_hashes=tuple(sha for _, sha in selected),
                )
            )
    return requirements


def _rank_candidates(session: Session, rows: Sequence[Any]) -> list[tuple[UUID, str]]:
    """Rank persisted candidates with the existing deterministic T19 selector."""
    if not rows:
        return []
    hashes: dict[UUID, str] = {}
    candidates: list[Any] = []
    for row in rows:
        asset = session.get(Asset, row["source_asset_id"])
        if asset is None:
            continue
        hashes[asset.id] = asset.sha256
        components = dict(row["score_components"] or {})
        scores = CandidateScores(
            evidence=float(components.get("evidence", row["score"])),
            visibility=float(components.get("visibility", row["score"])),
            sharpness=float(components.get("sharpness", row["score"])),
            exposure=float(components.get("exposure", row["score"])),
            obstruction=float(components.get("obstruction", 0.0)),
            state_relevance=float(components.get("state_relevance", row["score"])),
            diversity=float(components.get("diversity", row["score"])),
        )
        shared = {
            "asset_id": asset.id,
            "sha256": asset.sha256,
            "source_scene_id": row["source_scene_id"],
            "source_timestamp_ms": int(row["source_timestamp_ms"]),
            "scores": scores,
        }
        # The selector ranks on scores, timestamp and hash only, so one
        # candidate shape serves both entity kinds without a second branch.
        candidates.append(CharacterReferenceCandidate(**shared))
    ranked = select_candidates(candidates)
    return [(item.asset_id, hashes[item.asset_id]) for item in ranked]


class ContinuityReferenceOrchestrator:
    """Persist the T19 lifecycle for one project, idempotently and restartably."""

    def __init__(
        self,
        session: Session,
        *,
        generator: ReferenceSheetGenerator,
        provider: str,
        model: str,
    ) -> None:
        self._session = session
        self._generator = generator
        self._provider = provider
        self._model = model

    # -- build ------------------------------------------------------------
    def build(
        self,
        *,
        project_id: UUID,
        episode_analysis_id: UUID,
        reference_run_id: UUID,
        idempotency_key: str,
        entity_id: UUID | None = None,
    ) -> ReferenceBuildOutcome:
        """Draft (or reuse) every reference sheet this project requires.

        ``entity_id`` narrows the build to one character or location, which is
        what the per-entity regeneration endpoint dispatches. Siblings keep
        their approved sheets untouched.
        """
        requirements = [
            requirement
            for requirement in resolve_requirements(self._session, project_id)
            if entity_id is None or requirement.entity_id == entity_id
        ]
        if entity_id is not None and not requirements:
            raise ContinuityOrchestrationError(
                "reference_entity_not_found",
                "The requested character or location is not part of this project.",
            )
        drafted: list[UUID] = []
        reused: list[UUID] = []
        for requirement in requirements:
            if not requirement.required:
                continue
            identity_version_id = self._upsert_identity_version(
                project_id=project_id,
                episode_analysis_id=episode_analysis_id,
                requirement=requirement,
            )
            reference_set_id, was_reused = self._ensure_reference_sheet(
                project_id=project_id,
                requirement=requirement,
                identity_version_id=identity_version_id,
                idempotency_key=f"{idempotency_key}:{requirement.kind}:{requirement.entity_id}",
            )
            (reused if was_reused else drafted).append(reference_set_id)
        self._session.flush()
        pending = self._pending_approvals(project_id)
        return ReferenceBuildOutcome(
            reference_run_id=reference_run_id,
            draft_reference_set_ids=tuple(drafted),
            reused_reference_set_ids=tuple(reused),
            requires_approval=bool(pending),
            entity_count=sum(1 for item in requirements if item.required),
        )

    def _upsert_identity_version(
        self,
        *,
        project_id: UUID,
        episode_analysis_id: UUID,
        requirement: EntityRequirement,
    ) -> UUID:
        identities, _, _, entity_column = _candidate_tables(requirement.kind)
        observations = _observations(requirement.definition)
        evidence = [
            EvidenceLink(evidence_id=asset_id) for asset_id in requirement.candidate_asset_ids
        ]
        if requirement.kind == "character":
            bible: Any = build_character_bible(
                character_id=requirement.entity_id,
                display_name=requirement.display_name,
                aliases=[],
                observations=observations,
                evidence=evidence,
                confidence=1.0,
            )
        else:
            bible = build_location_bible(
                location_id=requirement.entity_id,
                display_name=requirement.display_name,
                location_type=str(requirement.definition.get("location_type") or "") or None,
                observations=observations,
                evidence=evidence,
                confidence=1.0,
            )
        payload = bible.model_dump(mode="json")
        digest = canonical_hash({"bible": payload, "orchestrator_version": ORCHESTRATOR_VERSION})
        existing = self._session.execute(
            select(identities.c.id).where(
                identities.c.project_id == project_id, identities.c.identity_hash == digest
            )
        ).scalar_one_or_none()
        if existing is not None:
            return UUID(str(existing))
        version = (
            int(
                self._session.execute(
                    select(identities.c.version)
                    .where(identities.c[entity_column] == requirement.entity_id)
                    .order_by(identities.c.version.desc())
                    .limit(1)
                ).scalar_one_or_none()
                or 0
            )
            + 1
        )
        moment = _now()
        identity_version_id = uuid4()
        self._session.execute(
            insert(identities).values(
                id=identity_version_id,
                project_id=project_id,
                **{entity_column: requirement.entity_id},
                episode_analysis_id=episode_analysis_id,
                version=version,
                identity=payload,
                identity_hash=digest,
                status="draft",
                created_at=moment,
                updated_at=moment,
            )
        )
        return identity_version_id

    def _ensure_reference_sheet(
        self,
        *,
        project_id: UUID,
        requirement: EntityRequirement,
        identity_version_id: UUID,
        idempotency_key: str,
    ) -> tuple[UUID, bool]:
        """Generate one sheet, or adopt the sheet that identity already has."""
        _, _, reference_sets, _ = _candidate_tables(requirement.kind)
        existing = (
            self._session.execute(
                select(reference_sets).where(
                    reference_sets.c.identity_version_id == identity_version_id,
                    reference_sets.c.status.in_(["draft", "approved"]),
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return UUID(str(existing["id"])), True
        request = ReferenceGenerationRequest(
            project_id=project_id,
            identity_version_id=identity_version_id,
            entity_kind=requirement.kind,
            ordered_source_asset_ids=list(requirement.candidate_asset_ids),
            provider=self._provider,
            model=self._model,
            generation_parameters={"orchestrator_version": ORCHESTRATOR_VERSION},
            idempotency_key=idempotency_key[:255],
        )
        result = self._generator(request, list(requirement.candidate_hashes))
        if not result.validation.valid:
            raise ContinuityOrchestrationError(
                "reference_validation_failed",
                f"The generated reference sheet for {requirement.display_name} failed validation.",
            )
        # A concurrent build may have produced the same identity first; the
        # unique index on ``reference_identity`` is what makes that safe, so
        # adopt the winner rather than inserting a duplicate.
        already = self._session.execute(
            select(reference_sets.c.id).where(
                reference_sets.c.reference_identity == result.reference_identity
            )
        ).scalar_one_or_none()
        if already is not None:
            return UUID(str(already)), True
        moment = _now()
        reference_set_id = uuid4()
        self._session.execute(
            insert(reference_sets).values(
                id=reference_set_id,
                project_id=project_id,
                identity_version_id=identity_version_id,
                reference_identity=result.reference_identity,
                status="draft",
                provider_attempt_id=result.provider_attempt_id,
                primary_asset_id=result.asset_id,
                ordered_asset_ids=[str(result.asset_id)],
                validation_report=result.validation.model_dump(mode="json"),
                row_version=1,
                created_at=moment,
                updated_at=moment,
            )
        )
        return reference_set_id, False

    def _pending_approvals(self, project_id: UUID) -> list[UUID]:
        pending: list[UUID] = []
        for table in (character_reference_sets, location_reference_sets):
            pending.extend(
                UUID(str(value))
                for value in self._session.scalars(
                    select(table.c.id).where(
                        table.c.project_id == project_id, table.c.status == "draft"
                    )
                )
            )
        return pending

    # -- apply ------------------------------------------------------------
    def apply(
        self,
        *,
        project_id: UUID,
        storyboard_run_id: UUID,
        idempotency_key: str,
        regenerate_shot: Callable[[UUID, str, str], None] | None = None,
    ) -> ReferenceApplyOutcome:
        """Bind approved references onto every shot that depends on them.

        Refuses to bind while any sheet is still a draft: a shot must never be
        generated against a reference nobody approved.
        """
        pending = self._pending_approvals(project_id)
        if pending:
            raise ContinuityOrchestrationError(
                "reference_approval_pending",
                f"{len(pending)} reference sheet(s) are still awaiting approval.",
            )
        approved = self._approved_references(project_id)
        bundles = self._bind(project_id, storyboard_run_id, approved)
        changed = self._persist_bindings(project_id, storyboard_run_id, bundles)
        invalidation: ReferenceInvalidation | None = None
        regenerated: tuple[UUID, ...] = ()
        if changed and regenerate_shot is not None:
            started: list[UUID] = []

            def record(shot_id: UUID, bundle_hash: str, key: str) -> None:
                started.append(shot_id)
                regenerate_shot(shot_id, bundle_hash, key)

            invalidation = ContinuityRegenerator(self._session, record).apply(
                project_id=project_id, bundles=changed, idempotency_key=idempotency_key
            )
            regenerated = tuple(started)
        self._session.flush()
        return ReferenceApplyOutcome(
            project_id=project_id,
            bound_shot_ids=tuple(bundle.shot_id for bundle in bundles),
            invalidation=invalidation,
            approved_reference_set_ids=tuple(sorted(approved, key=str)),
            regenerated_shot_ids=regenerated,
        )

    def _approved_references(self, project_id: UUID) -> dict[UUID, tuple[UUID, UUID, str, str]]:
        """Map entity ID -> (reference set ID, asset ID, sha256, kind)."""
        resolved: dict[UUID, tuple[UUID, UUID, str, str]] = {}
        for kind in ("character", "location"):
            identities, _, reference_sets, entity_column = _candidate_tables(kind)  # type: ignore[arg-type]
            rows = self._session.execute(
                select(
                    reference_sets.c.id,
                    reference_sets.c.primary_asset_id,
                    identities.c[entity_column],
                )
                .join(identities, reference_sets.c.identity_version_id == identities.c.id)
                .where(
                    reference_sets.c.project_id == project_id,
                    reference_sets.c.status == "approved",
                )
            ).all()
            for reference_set_id, asset_id, entity_id in rows:
                asset = self._session.get(Asset, asset_id) if asset_id else None
                if asset is None:
                    continue
                resolved[UUID(str(entity_id))] = (
                    UUID(str(reference_set_id)),
                    asset.id,
                    asset.sha256,
                    kind,
                )
        return resolved

    def _bind(
        self,
        project_id: UUID,
        storyboard_run_id: UUID,
        approved: dict[UUID, tuple[UUID, UUID, str, str]],
    ) -> list[Any]:
        shots: list[CanonicalShotReferences] = []
        for shot in self._session.scalars(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == storyboard_run_id)
            .order_by(StoryboardShotRecord.global_sequence)
        ):
            references: list[ReferenceBundleItem] = []
            entity_ids = [
                UUID(str(value))
                for value in (shot.references or {}).get("character_reference_ids", [])
            ]
            location_id = (shot.references or {}).get("location_reference_id")
            if location_id:
                entity_ids.append(UUID(str(location_id)))
            for priority, entity_id in enumerate(entity_ids):
                resolved = approved.get(entity_id)
                if resolved is None:
                    continue
                _, asset_id, sha256, kind = resolved
                references.append(
                    ReferenceBundleItem(
                        asset_id=asset_id,
                        sha256=sha256,
                        role="character_identity" if kind == "character" else "location_identity",
                        entity_id=entity_id,
                        required=True,
                        priority=priority,
                    )
                )
            shots.append(
                CanonicalShotReferences(
                    shot_id=shot.stable_shot_id,
                    sequence=shot.global_sequence,
                    references=tuple(references),
                )
            )
        return ContinuityPipeline().bind_shots(
            project_id=project_id,
            storyboard_run_id=storyboard_run_id,
            shots=shots,
            provider_reference_limit=PROVIDER_REFERENCE_LIMIT,
        )

    def _persist_bindings(
        self, project_id: UUID, storyboard_run_id: UUID, bundles: Sequence[Any]
    ) -> list[Any]:
        """Write each shot's bundle, returning only the ones that changed.

        An unchanged bundle is left exactly as it was, which is what stops an
        approval of one entity from restarting every shot in the project.
        """
        changed: list[Any] = []
        for bundle in bundles:
            shot_row = self._session.scalar(
                select(StoryboardShotRecord).where(
                    StoryboardShotRecord.storyboard_run_id == storyboard_run_id,
                    StoryboardShotRecord.stable_shot_id == bundle.shot_id,
                )
            )
            if shot_row is None:  # pragma: no cover - bundles come from these rows
                continue
            current = (
                self._session.execute(
                    select(shot_reference_bindings).where(
                        shot_reference_bindings.c.project_id == project_id,
                        shot_reference_bindings.c.storyboard_shot_id == shot_row.id,
                    )
                )
                .mappings()
                .first()
            )
            payload = bundle.model_dump(mode="json")
            moment = _now()
            if current is None:
                self._session.execute(
                    insert(shot_reference_bindings).values(
                        id=uuid4(),
                        project_id=project_id,
                        storyboard_id=storyboard_run_id,
                        storyboard_shot_id=shot_row.id,
                        bundle=payload,
                        bundle_hash=bundle.bundle_hash,
                        status="bound",
                        created_at=moment,
                        updated_at=moment,
                    )
                )
                changed.append(bundle)
                continue
            if current["bundle_hash"] == bundle.bundle_hash:
                continue
            self._session.execute(
                update(shot_reference_bindings)
                .where(shot_reference_bindings.c.id == current["id"])
                .values(bundle=payload, bundle_hash=bundle.bundle_hash, updated_at=moment)
            )
            changed.append(bundle)
        return changed
