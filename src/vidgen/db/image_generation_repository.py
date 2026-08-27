"""Authoritative T13 selection and restartable T14 persistence."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.models import Asset, Project
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.storyboard_repository import StoryboardRepository


class ImageGenerationLineageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SelectedStoryboard:
    project: Project
    storyboard: StoryboardRun
    shots: tuple[StoryboardShotRecord, ...]
    storyboard_asset: Asset
    timing_asset: Asset


class ImageGenerationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def selected_storyboard(
        self, project_id: UUID, storyboard_id: UUID | None = None
    ) -> SelectedStoryboard:
        project = self.session.get(Project, project_id)
        if project is None:
            raise ImageGenerationLineageError("project_missing", "project does not exist")
        query = select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected
        )
        if storyboard_id is not None:
            query = query.where(StoryboardRun.id == storyboard_id)
        run = self.session.scalar(query)
        if run is None:
            raise ImageGenerationLineageError(
                "storyboard_unselected", "requested project has no matching selected storyboard"
            )
        if run.status != "storyboard_complete":
            raise ImageGenerationLineageError(
                "storyboard_incomplete", f"selected storyboard status is {run.status!r}"
            )
        # Re-evaluate T10-T12 selection. This rejects a storyboard made stale by
        # any newly selected upstream entity rather than silently mixing versions.
        upstream = StoryboardRepository(self.session).authoritative_inputs(project_id)
        if (
            run.episode_model_id != upstream.episode_model.id
            or run.script_id != upstream.script.id
            or run.script_version != upstream.script.version
            or run.narration_run_id != upstream.narration_run.id
        ):
            raise ImageGenerationLineageError(
                "storyboard_stale", "selected storyboard does not match selected T10-T12 lineage"
            )
        if run.storyboard_asset_id is None or run.timing_manifest_asset_id is None:
            raise ImageGenerationLineageError(
                "storyboard_artifacts_missing", "canonical storyboard or timing manifest is missing"
            )
        storyboard_asset = self.session.get(Asset, run.storyboard_asset_id)
        timing_asset = self.session.get(Asset, run.timing_manifest_asset_id)
        if storyboard_asset is None or timing_asset is None:
            raise ImageGenerationLineageError(
                "storyboard_artifacts_missing", "canonical lineage assets no longer exist"
            )
        if storyboard_asset.project_id != project_id or timing_asset.project_id != project_id:
            raise ImageGenerationLineageError(
                "storyboard_cross_project", "canonical lineage assets belong to another project"
            )
        shots = tuple(StoryboardRepository(self.session).shots(run.id))
        if not shots or len(shots) != run.shot_count:
            raise ImageGenerationLineageError(
                "storyboard_shots_incomplete",
                "canonical shot count does not match the selected run",
            )
        if [shot.global_sequence for shot in shots] != list(range(len(shots))):
            raise ImageGenerationLineageError(
                "storyboard_timing_invalid", "shot sequence is not dense and canonical"
            )
        cursor = 0
        for shot in shots:
            if shot.global_start_us != cursor or shot.global_end_us <= shot.global_start_us:
                raise ImageGenerationLineageError(
                    "storyboard_timing_invalid", f"shot {shot.id} breaks gapless timing"
                )
            if shot.contract.get("capability_hash") != run.capability_hash:
                raise ImageGenerationLineageError(
                    "capability_hash_mismatch", f"shot {shot.id} capability hash is stale"
                )
            cursor = shot.global_end_us
        if cursor != run.total_duration_us:
            raise ImageGenerationLineageError(
                "storyboard_timing_invalid", "shot coverage differs from timing manifest duration"
            )
        return SelectedStoryboard(project, run, shots, storyboard_asset, timing_asset)

    def run_by_key(self, project_id: UUID, key: str) -> ImageGenerationRun | None:
        return self.session.scalar(
            select(ImageGenerationRun).where(
                ImageGenerationRun.project_id == project_id,
                ImageGenerationRun.idempotency_key == key,
            )
        )

    def item_by_identity(self, identity: str) -> ImageGenerationItem | None:
        return self.session.scalar(
            select(ImageGenerationItem).where(ImageGenerationItem.generation_identity == identity)
        )

    def items(self, run_id: UUID) -> list[ImageGenerationItem]:
        return list(
            self.session.scalars(
                select(ImageGenerationItem)
                .where(ImageGenerationItem.run_id == run_id)
                .order_by(ImageGenerationItem.shot_sequence, ImageGenerationItem.keyframe_role)
            )
        )

    def generated(self, item_id: UUID) -> GeneratedKeyframeImage | None:
        return self.session.scalar(
            select(GeneratedKeyframeImage).where(GeneratedKeyframeImage.item_id == item_id)
        )
