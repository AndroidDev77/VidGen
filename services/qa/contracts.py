"""Authoritative T20 input selection and structured lineage failures.

Nothing in T20 evaluates an asset it has not first proved belongs to the
requested project, the selected T13 storyboard, the selected T14/T15 attempt,
and the exact approved T19 reference versions. Every rejection here is a
structured, non-retryable failure: a stale or mixed lineage is a configuration
problem, not something a retry can fix.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.storyboard import StoryboardShot
from vidgen.contracts.visual_qa import (
    VisualQAFailure,
    VisualQAFailureCode,
    VisualQAShotImportance,
    VisualQATarget,
    VisualQATargetType,
)
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_sets,
    character_state_snapshots,
    location_identity_versions,
    location_reference_sets,
    location_state_snapshots,
    shot_reference_bindings,
)
from vidgen.db.image_generation_models import GeneratedKeyframeImage
from vidgen.db.models import Asset, Project
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord

#: Hero shots are classified by the T13 importance provenance the Director emits.
HERO_IMPORTANCE_FLOOR: Final = 0.8
UTILITY_IMPORTANCE_CEILING: Final = 0.3
SUPPORTED_KEYFRAME_MEDIA: Final = frozenset({"image/png", "image/jpeg", "image/webp"})
SUPPORTED_VIDEO_MEDIA: Final = frozenset({"video/mp4"})
COMPLETE_STORYBOARD_STATUSES: Final = frozenset({"storyboard_complete", "completed"})


class VisualQALineageError(ValueError):
    """A structured, non-retryable T20 input-selection failure."""

    def __init__(
        self, code: VisualQAFailureCode, message: str, *, reference_id: UUID | None = None
    ) -> None:
        super().__init__(message)
        self.failure = VisualQAFailure(
            code=code, message=message[:500], retryable=False, reference_id=reference_id
        )

    @property
    def code(self) -> VisualQAFailureCode:
        return self.failure.code

    @property
    def retryable(self) -> bool:
        return False


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """One approved T19 reference asset with the version that approved it."""

    asset_id: UUID
    sha256: str
    role: str
    entity_id: UUID
    identity_version_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class AuthoritativeQAInputs:
    """Everything one QA run may read, already proved compatible."""

    project: Project
    storyboard: StoryboardRun
    shot_record: StoryboardShotRecord
    shot: StoryboardShot
    importance: VisualQAShotImportance
    target_type: VisualQATargetType
    target_asset: Asset
    keyframe: GeneratedKeyframeImage | None
    video: AnimationGeneratedVideo | None
    timing_manifest_asset: Asset
    shot_workflow_identity: str
    bundle: dict[str, Any]
    bundle_hash: str
    references: tuple[ResolvedReference, ...]
    character_identity_version_ids: tuple[UUID, ...]
    location_identity_version_id: UUID | None
    character_state_snapshots: tuple[dict[str, Any], ...]
    location_state_snapshot: dict[str, Any] | None
    previous_shot_record: StoryboardShotRecord | None
    previous_video: AnimationGeneratedVideo | None

    @property
    def character_state_hashes(self) -> tuple[str, ...]:
        return tuple(str(item["snapshot_hash"]) for item in self.character_state_snapshots)

    @property
    def location_state_hash(self) -> str | None:
        if self.location_state_snapshot is None:
            return None
        return str(self.location_state_snapshot["snapshot_hash"])

    def target(self) -> VisualQATarget:
        return VisualQATarget(
            project_id=self.project.id,
            storyboard_run_id=self.storyboard.id,
            storyboard_shot_id=self.shot_record.id,
            shot_sequence=self.shot_record.global_sequence,
            target_type=self.target_type,
            target_asset_id=self.target_asset.id,
            target_asset_sha256=self.target_asset.sha256,
            media_type=self.target_asset.media_type,
            shot_workflow_identity=self.shot_workflow_identity,
            canonical_shot_hash=canonical_hash(self.shot_record.contract),
            shot_reference_bundle_hash=self.bundle_hash,
            importance=self.importance,
            usable_duration_us=self.shot_record.usable_duration_us,
            requested_generation_duration_us=self.shot_record.requested_generation_duration_us,
            character_identity_version_ids=list(self.character_identity_version_ids),
            character_reference_asset_ids=[
                item.asset_id
                for item in self.references
                if item.role in {"character_identity", "character_state"}
            ],
            location_identity_version_id=self.location_identity_version_id,
            location_reference_asset_ids=[
                item.asset_id
                for item in self.references
                if item.role in {"location_identity", "location_state"}
            ],
            character_state_snapshot_hashes=list(self.character_state_hashes),
            location_state_snapshot_hash=self.location_state_hash,
            required_props=list(self.shot.prop_references),
        )


def classify_importance(shot: StoryboardShot) -> VisualQAShotImportance:
    """Classify a shot from the T13 provenance the Director already persisted."""
    raw = shot.provenance.get("importance", 0.5)
    try:
        importance = float(raw)
    except (TypeError, ValueError):
        importance = 0.5
    if importance >= HERO_IMPORTANCE_FLOOR:
        return VisualQAShotImportance.HERO
    if importance <= UTILITY_IMPORTANCE_CEILING:
        return VisualQAShotImportance.UTILITY
    return VisualQAShotImportance.NORMAL


class AuthoritativeInputSelector:
    """Load and verify every input one QA run is permitted to evaluate."""

    def __init__(self, session: Session, *, shot_workflow_identity_resolver: Any) -> None:
        self._session = session
        self._identity = shot_workflow_identity_resolver

    def select(
        self, project_id: UUID, shot_id: UUID, target_type: VisualQATargetType
    ) -> AuthoritativeQAInputs:
        project = self._session.get(Project, project_id)
        if project is None:
            raise VisualQALineageError(
                VisualQAFailureCode.PROJECT_NOT_FOUND,
                "requested project does not exist",
                reference_id=project_id,
            )
        storyboard = self._session.scalar(
            select(StoryboardRun).where(
                StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
            )
        )
        if storyboard is None:
            raise VisualQALineageError(
                VisualQAFailureCode.STORYBOARD_NOT_SELECTED,
                "project has no selected T13 storyboard",
            )
        # T13 writes ``storyboard_complete``; T17's selection and the review
        # fixtures use ``completed``. Both mean the same selected, finished run.
        if storyboard.status not in COMPLETE_STORYBOARD_STATUSES:
            raise VisualQALineageError(
                VisualQAFailureCode.STALE_STORYBOARD_VERSION,
                "selected T13 storyboard is not complete",
                reference_id=storyboard.id,
            )
        shot_record = self._session.scalar(
            select(StoryboardShotRecord).where(
                StoryboardShotRecord.storyboard_run_id == storyboard.id,
                StoryboardShotRecord.id == shot_id,
            )
        ) or self._session.scalar(
            select(StoryboardShotRecord).where(
                StoryboardShotRecord.storyboard_run_id == storyboard.id,
                StoryboardShotRecord.stable_shot_id == shot_id,
            )
        )
        if shot_record is None:
            raise VisualQALineageError(
                VisualQAFailureCode.SHOT_NOT_FOUND,
                "shot is not part of the selected storyboard",
                reference_id=shot_id,
            )
        try:
            shot = StoryboardShot.model_validate(shot_record.contract)
        except ValueError as error:
            # A shot row whose canonical contract no longer validates is a stale
            # or mixed lineage, not something a retry can fix.
            raise VisualQALineageError(
                VisualQAFailureCode.MIXED_LINEAGE,
                "canonical T13 shot contract does not validate against the current schema",
                reference_id=shot_record.id,
            ) from error
        if storyboard.timing_manifest_asset_id is None:
            raise VisualQALineageError(
                VisualQAFailureCode.MISSING_QA_CONFIGURATION,
                "selected storyboard has no timing manifest asset",
                reference_id=storyboard.id,
            )
        timing_asset = self._require_asset(
            storyboard.timing_manifest_asset_id, project_id, "timing manifest"
        )
        keyframe, video, target_asset = self._select_generation(
            project_id, shot_record, storyboard, target_type
        )
        bundle_row = self._select_bundle(project_id, storyboard.id, shot_record.id)
        references, character_versions, location_version = self._resolve_references(
            project_id, bundle_row["bundle"]
        )
        character_states, location_state = self._resolve_state_snapshots(
            shot_record.id, bundle_row["bundle"]
        )
        previous_record, previous_video = self._previous_shot(storyboard.id, shot_record)
        identity = self._identity(self._session, storyboard, shot_record)
        return AuthoritativeQAInputs(
            project=project,
            storyboard=storyboard,
            shot_record=shot_record,
            shot=shot,
            importance=classify_importance(shot),
            target_type=target_type,
            target_asset=target_asset,
            keyframe=keyframe,
            video=video,
            timing_manifest_asset=timing_asset,
            shot_workflow_identity=identity,
            bundle=bundle_row["bundle"],
            bundle_hash=str(bundle_row["bundle_hash"]),
            references=references,
            character_identity_version_ids=character_versions,
            location_identity_version_id=location_version,
            character_state_snapshots=character_states,
            location_state_snapshot=location_state,
            previous_shot_record=previous_record,
            previous_video=previous_video,
        )

    def _require_asset(self, asset_id: UUID, project_id: UUID, label: str) -> Asset:
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise VisualQALineageError(
                VisualQAFailureCode.MISSING_QA_CONFIGURATION,
                f"{label} asset is missing",
                reference_id=asset_id,
            )
        if asset.project_id != project_id:
            raise VisualQALineageError(
                VisualQAFailureCode.CROSS_PROJECT_ASSET,
                f"{label} asset belongs to another project",
                reference_id=asset_id,
            )
        return asset

    def _select_generation(
        self,
        project_id: UUID,
        shot_record: StoryboardShotRecord,
        storyboard: StoryboardRun,
        target_type: VisualQATargetType,
    ) -> tuple[GeneratedKeyframeImage | None, AnimationGeneratedVideo | None, Asset]:
        if target_type is VisualQATargetType.KEYFRAME:
            keyframe = self._session.scalar(
                select(GeneratedKeyframeImage).where(
                    GeneratedKeyframeImage.shot_id == shot_record.id,
                    GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
                    GeneratedKeyframeImage.selected.is_(True),
                )
            )
            if keyframe is None:
                raise VisualQALineageError(
                    VisualQAFailureCode.MISSING_KEYFRAME,
                    "shot has no selected T14 first keyframe",
                    reference_id=shot_record.id,
                )
            if keyframe.project_id != project_id:
                raise VisualQALineageError(
                    VisualQAFailureCode.CROSS_PROJECT_ASSET,
                    "selected keyframe belongs to another project",
                    reference_id=keyframe.id,
                )
            asset = self._require_asset(keyframe.asset_id, project_id, "keyframe")
            if asset.sha256 != keyframe.sha256:
                raise VisualQALineageError(
                    VisualQAFailureCode.ASSET_HASH_MISMATCH,
                    "keyframe asset hash does not match the persisted generation record",
                    reference_id=keyframe.id,
                )
            if asset.media_type not in SUPPORTED_KEYFRAME_MEDIA:
                raise VisualQALineageError(
                    VisualQAFailureCode.UNSUPPORTED_MEDIA,
                    f"unsupported keyframe media type {asset.media_type}",
                    reference_id=asset.id,
                )
            return keyframe, None, asset
        video = self._session.scalar(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot_record.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        )
        if video is None:
            raise VisualQALineageError(
                VisualQAFailureCode.MISSING_CANONICAL_VIDEO,
                "shot has no selected canonical T15 video",
                reference_id=shot_record.id,
            )
        if video.project_id != project_id:
            raise VisualQALineageError(
                VisualQAFailureCode.CROSS_PROJECT_ASSET,
                "selected canonical video belongs to another project",
                reference_id=video.id,
            )
        run = self._session.scalar(
            select(AnimationRun)
            .join(AnimationItem, AnimationItem.run_id == AnimationRun.id)
            .where(
                AnimationItem.id == video.animation_item_id,
                AnimationRun.storyboard_id == storyboard.id,
            )
        )
        if run is None:
            raise VisualQALineageError(
                VisualQAFailureCode.MIXED_LINEAGE,
                "selected clip does not belong to a T15 run for the selected storyboard",
                reference_id=video.id,
            )
        if run.storyboard_version != storyboard.version:
            raise VisualQALineageError(
                VisualQAFailureCode.STALE_GENERATION_ATTEMPT,
                "selected clip was generated from a different storyboard version",
                reference_id=video.id,
            )
        item = self._session.get(AnimationItem, video.animation_item_id)
        if item is None or item.selected_generated_video_id != video.id:
            raise VisualQALineageError(
                VisualQAFailureCode.UNSELECTED_GENERATION_ATTEMPT,
                "canonical video is not the selected attempt for its T15 item",
                reference_id=video.id,
            )
        asset = self._require_asset(video.canonical_asset_id, project_id, "canonical video")
        if asset.sha256 != video.sha256:
            raise VisualQALineageError(
                VisualQAFailureCode.ASSET_HASH_MISMATCH,
                "canonical video asset hash does not match the persisted generation record",
                reference_id=video.id,
            )
        if asset.media_type not in SUPPORTED_VIDEO_MEDIA:
            raise VisualQALineageError(
                VisualQAFailureCode.UNSUPPORTED_MEDIA,
                f"unsupported video media type {asset.media_type}",
                reference_id=asset.id,
            )
        return None, video, asset

    def _select_bundle(
        self, project_id: UUID, storyboard_id: UUID, shot_id: UUID
    ) -> dict[str, Any]:
        row = (
            self._session.execute(
                select(shot_reference_bindings).where(
                    shot_reference_bindings.c.project_id == project_id,
                    shot_reference_bindings.c.storyboard_id == storyboard_id,
                    shot_reference_bindings.c.storyboard_shot_id == shot_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise VisualQALineageError(
                VisualQAFailureCode.MISSING_REFERENCE_BUNDLE,
                "shot has no immutable T19 shot-reference bundle",
                reference_id=shot_id,
            )
        return dict(row)

    def _resolve_references(
        self, project_id: UUID, bundle: dict[str, Any]
    ) -> tuple[tuple[ResolvedReference, ...], tuple[UUID, ...], UUID | None]:
        character_version_ids = tuple(
            UUID(str(value)) for value in bundle.get("character_identity_version_ids", [])
        )
        raw_location = bundle.get("location_identity_version_id")
        location_version_id = UUID(str(raw_location)) if raw_location else None
        approved_characters = self._approved_versions(
            character_identity_versions, project_id, character_version_ids
        )
        approved_locations = self._approved_versions(
            location_identity_versions,
            project_id,
            (location_version_id,) if location_version_id else (),
        )
        approved_assets = self._approved_reference_assets(
            character_reference_sets, character_version_ids
        ) | self._approved_reference_assets(
            location_reference_sets, (location_version_id,) if location_version_id else ()
        )
        resolved: list[ResolvedReference] = []
        for item in bundle.get("references", []):
            asset_id = UUID(str(item["asset_id"]))
            role = str(item["role"])
            entity_id = UUID(str(item["entity_id"]))
            if role == "prop":
                continue
            version_id = (
                location_version_id
                if role.startswith("location")
                else self._character_version_for(entity_id, approved_characters)
            )
            if version_id is None:
                raise VisualQALineageError(
                    VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION,
                    "bundle references an entity without an approved T19 identity version",
                    reference_id=entity_id,
                )
            if asset_id not in approved_assets:
                raise VisualQALineageError(
                    VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION,
                    "bundle references an asset outside the approved T19 reference set",
                    reference_id=asset_id,
                )
            asset = self._require_asset(asset_id, project_id, "T19 reference")
            if asset.sha256 != str(item["sha256"]):
                raise VisualQALineageError(
                    VisualQAFailureCode.ASSET_HASH_MISMATCH,
                    "T19 reference asset hash does not match the bundle",
                    reference_id=asset_id,
                )
            resolved.append(
                ResolvedReference(
                    asset_id=asset_id,
                    sha256=asset.sha256,
                    role=role,
                    entity_id=entity_id,
                    identity_version_id=version_id,
                    label=role,
                )
            )
        if location_version_id is not None and not approved_locations:
            raise VisualQALineageError(
                VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION,
                "bundle location identity version is not approved",
                reference_id=location_version_id,
            )
        return tuple(resolved), character_version_ids, location_version_id

    def _character_version_for(self, entity_id: UUID, approved: dict[UUID, UUID]) -> UUID | None:
        return approved.get(entity_id)

    def _approved_versions(
        self, table: Any, project_id: UUID, version_ids: tuple[UUID, ...]
    ) -> dict[UUID, UUID]:
        if not version_ids:
            return {}
        entity_column = (
            table.c.character_id if "character_id" in table.c.keys() else table.c.location_id
        )
        rows = (
            self._session.execute(
                select(table).where(
                    table.c.project_id == project_id, table.c.id.in_(list(version_ids))
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != len(set(version_ids)):
            raise VisualQALineageError(
                VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION,
                "bundle binds an identity version that does not belong to this project",
            )
        approved: dict[UUID, UUID] = {}
        for row in rows:
            if row["status"] != "approved":
                raise VisualQALineageError(
                    VisualQAFailureCode.INCOMPATIBLE_REFERENCE_VERSION,
                    "bundle binds an identity version that is not approved",
                    reference_id=row["id"],
                )
            approved[row[entity_column.name]] = row["id"]
        return approved

    def _approved_reference_assets(self, table: Any, version_ids: tuple[UUID, ...]) -> set[UUID]:
        if not version_ids:
            return set()
        rows = (
            self._session.execute(
                select(table).where(table.c.identity_version_id.in_(list(version_ids)))
            )
            .mappings()
            .all()
        )
        assets: set[UUID] = set()
        for row in rows:
            if row["status"] != "approved":
                continue
            assets.update(UUID(str(value)) for value in row["ordered_asset_ids"])
        return assets

    def _resolve_state_snapshots(
        self, shot_id: UUID, bundle: dict[str, Any]
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, Any] | None]:
        expected = [UUID(str(value)) for value in bundle.get("character_state_snapshot_ids", [])]
        rows = (
            self._session.execute(
                select(character_state_snapshots).where(
                    character_state_snapshots.c.storyboard_shot_id == shot_id
                )
            )
            .mappings()
            .all()
        )
        by_id = {row["id"]: dict(row) for row in rows}
        characters: list[dict[str, Any]] = []
        for snapshot_id in expected:
            snapshot = by_id.get(snapshot_id)
            if snapshot is None:
                raise VisualQALineageError(
                    VisualQAFailureCode.MISSING_STATE_SNAPSHOT,
                    "required T19 character state snapshot is missing for this shot",
                    reference_id=snapshot_id,
                )
            characters.append(snapshot)
        raw_location = bundle.get("location_state_snapshot_id")
        location: dict[str, Any] | None = None
        if raw_location:
            location_row = (
                self._session.execute(
                    select(location_state_snapshots).where(
                        location_state_snapshots.c.id == UUID(str(raw_location)),
                        location_state_snapshots.c.storyboard_shot_id == shot_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if location_row is None:
                raise VisualQALineageError(
                    VisualQAFailureCode.MISSING_STATE_SNAPSHOT,
                    "required T19 location state snapshot is missing for this shot",
                    reference_id=UUID(str(raw_location)),
                )
            location = dict(location_row)
        return tuple(characters), location

    def _previous_shot(
        self, storyboard_id: UUID, shot_record: StoryboardShotRecord
    ) -> tuple[StoryboardShotRecord | None, AnimationGeneratedVideo | None]:
        """Return the immediately preceding shot and its selected compatible clip."""
        if shot_record.global_sequence == 0:
            return None, None
        previous = self._session.scalar(
            select(StoryboardShotRecord).where(
                StoryboardShotRecord.storyboard_run_id == storyboard_id,
                StoryboardShotRecord.global_sequence == shot_record.global_sequence - 1,
            )
        )
        if previous is None:
            return None, None
        video = self._session.scalar(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == previous.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        )
        return previous, video
