"""Authoritative T13/T14 selection and durable T15 checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from vidgen.db.animation_models import (
    AnimationGeneratedVideo,
    AnimationItem,
    AnimationRun,
    RunwayTask,
)
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.image_generation_repository import ImageGenerationRepository, SelectedStoryboard
from vidgen.db.models import Asset


class AnimationLineageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SelectedKeyframes:
    first: GeneratedKeyframeImage
    first_asset: Asset
    last: GeneratedKeyframeImage | None
    last_asset: Asset | None


@dataclass(frozen=True, slots=True)
class AnimationInputs:
    storyboard: SelectedStoryboard
    image_run: ImageGenerationRun
    keyframes: dict[UUID, SelectedKeyframes]


class AnimationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authoritative_inputs(
        self,
        project_id: UUID,
        *,
        storyboard_id: UUID | None = None,
        image_run_id: UUID | None = None,
        shot_id: UUID | None = None,
    ) -> AnimationInputs:
        selected = ImageGenerationRepository(self.session).selected_storyboard(
            project_id, storyboard_id
        )
        base_query = select(ImageGenerationRun).where(
            ImageGenerationRun.project_id == project_id,
            ImageGenerationRun.storyboard_id == selected.storyboard.id,
            ImageGenerationRun.storyboard_version == selected.storyboard.version,
            ImageGenerationRun.status == "keyframes_complete",
        )
        # shot_id may be either the shot's primary key or its stable_shot_id
        # (production_handlers passes request.storyboard_shot_id, which is the
        # stable_shot_id). Resolve to the primary key so the ImageGenerationItem
        # FK join and the keyframe lookup both use the correct UUID.
        shot_pk_id: UUID | None = None
        if shot_id is not None:
            shot_record = next(
                (s for s in selected.shots if s.id == shot_id or s.stable_shot_id == shot_id),
                None,
            )
            if shot_record is None:
                raise AnimationLineageError(
                    "shot_not_in_storyboard",
                    f"shot {shot_id} is not part of the selected storyboard",
                )
            shot_pk_id = shot_record.id
        runs = list(self.session.scalars(base_query.order_by(ImageGenerationRun.created_at.desc())))
        if not runs:
            raise AnimationLineageError(
                "image_run_missing",
                "no completed T14 run matches the exact selected storyboard version",
            )
        authoritative = self._authoritative_run(base_query, shot_pk_id)
        if image_run_id is not None:
            requested = next((run for run in runs if run.id == image_run_id), None)
            if requested is None:
                raise AnimationLineageError(
                    "image_run_missing",
                    "no completed T14 run matches the exact selected storyboard version",
                )
            image_run = requested
        else:
            # A shot-scoped caller that names no run animates the run this shot
            # is authoritatively bound to, not the project's globally newest
            # one, which may hold nothing of this shot's.
            image_run = authoritative if authoritative is not None else runs[0]
        if authoritative is None or image_run.id != authoritative.id:
            raise AnimationLineageError("image_run_stale", "requested T14 run is not authoritative")
        if image_run.project_id != selected.project.id:
            raise AnimationLineageError(
                "image_run_cross_project", "T14 run belongs to another project"
            )

        # When targeting a specific shot, only validate keyframes for that shot.
        # Sibling shots' keyframes live in their own runs and are validated by
        # their own T15 activities; checking them here would always fail.
        shots_to_validate = (
            [s for s in selected.shots if s.id == shot_pk_id]
            if shot_id is not None
            else selected.shots
        )
        result: dict[UUID, SelectedKeyframes] = {}
        for shot in shots_to_validate:
            frames = list(
                self.session.scalars(
                    select(GeneratedKeyframeImage).where(
                        GeneratedKeyframeImage.shot_id == shot.id,
                        GeneratedKeyframeImage.selected,
                    )
                )
            )
            by_role = {frame.keyframe_role: frame for frame in frames}
            first = by_role.get("FIRST_FRAME")
            if first is None:
                raise AnimationLineageError(
                    "first_keyframe_missing", f"shot {shot.id} has no selected FIRST_FRAME"
                )
            if first.project_id != project_id:
                raise AnimationLineageError(
                    "keyframe_cross_project", f"shot {shot.id} keyframe belongs to another project"
                )
            first_item = self.session.get(ImageGenerationItem, first.item_id)
            if first_item is None or first_item.run_id != image_run.id:
                raise AnimationLineageError(
                    "keyframe_lineage_mismatch",
                    f"shot {shot.id} FIRST_FRAME was generated by a different T14 run",
                )
            if first.validation_report.get("valid") is not True:
                raise AnimationLineageError(
                    "keyframe_invalid",
                    f"shot {shot.id} selected FIRST_FRAME is technically invalid",
                )
            first_asset = self._asset(first, project_id)
            last = by_role.get("LAST_FRAME")
            if last is not None:
                last_item = self.session.get(ImageGenerationItem, last.item_id)
                if last_item is None or last_item.run_id != image_run.id:
                    raise AnimationLineageError(
                        "keyframe_lineage_mismatch",
                        f"shot {shot.id} LAST_FRAME was generated by a different T14 run",
                    )
            last_asset = self._asset(last, project_id) if last is not None else None
            result[shot.id] = SelectedKeyframes(first, first_asset, last, last_asset)
        return AnimationInputs(selected, image_run, result)

    def _authoritative_run(
        self, base_query: Select[tuple[ImageGenerationRun]], shot_pk_id: UUID | None
    ) -> ImageGenerationRun | None:
        """The T14 run that owns this shot's keyframes, or the project's newest.

        A project-wide caller animates the newest completed run, which owns
        every shot's keyframes at once.

        A shot-scoped caller cannot: each shot workflow creates its own
        ``ImageGenerationRun``, so the globally newest run may belong to a
        sibling shot (#39). Authority for one shot is therefore the run holding
        the item behind that shot's *selected* FIRST_FRAME keyframe - the very
        image T15 is about to animate. Defining it that way is what makes a
        replacement child work: its own T14 step may have found the keyframe
        material unchanged and reused the existing item, which belongs to the
        run that first generated it, leaving the replacement's own run holding
        no item at all. Such a run is not this shot's authority and must not
        become it, whether it was created before or after the selected keyframe.

        The fallback - the newest completed run with any item for this shot -
        covers a shot whose keyframe rows cannot name a run: it keeps the
        original scoping so an unrelated lineage break still reports itself as
        the missing or invalid keyframe it is rather than as a stale run.
        """
        if shot_pk_id is None:
            return self.session.scalar(base_query.order_by(ImageGenerationRun.created_at.desc()))
        owning_run = self.session.scalar(
            base_query.join(
                ImageGenerationItem, ImageGenerationItem.run_id == ImageGenerationRun.id
            )
            .join(
                GeneratedKeyframeImage,
                GeneratedKeyframeImage.item_id == ImageGenerationItem.id,
            )
            .where(
                GeneratedKeyframeImage.shot_id == shot_pk_id,
                GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
                GeneratedKeyframeImage.selected,
            )
        )
        if owning_run is not None:
            return owning_run
        return self.session.scalar(
            base_query.join(
                ImageGenerationItem, ImageGenerationItem.run_id == ImageGenerationRun.id
            )
            .where(ImageGenerationItem.shot_id == shot_pk_id)
            .order_by(ImageGenerationRun.created_at.desc())
        )

    def _asset(self, frame: GeneratedKeyframeImage, project_id: UUID) -> Asset:
        asset = self.session.get(Asset, frame.asset_id)
        if asset is None:
            raise AnimationLineageError(
                "keyframe_asset_missing", f"asset {frame.asset_id} is missing"
            )
        if asset.project_id != project_id:
            raise AnimationLineageError("keyframe_cross_project", "keyframe asset is cross-project")
        if asset.sha256 != frame.sha256:
            raise AnimationLineageError(
                "keyframe_hash_mismatch", "T14 and AssetService hashes differ"
            )
        return asset

    def run_by_key(self, project_id: UUID, key: str) -> AnimationRun | None:
        return self.session.scalar(
            select(AnimationRun).where(
                AnimationRun.project_id == project_id, AnimationRun.idempotency_key == key
            )
        )

    def item_by_identity(self, identity: str) -> AnimationItem | None:
        return self.session.scalar(
            select(AnimationItem).where(AnimationItem.generation_identity == identity)
        )

    def task_for_item(self, item_id: UUID) -> RunwayTask | None:
        return self.session.scalar(
            select(RunwayTask)
            .where(RunwayTask.animation_item_id == item_id)
            .order_by(RunwayTask.created_at.desc())
        )

    def video_for_item(self, item_id: UUID) -> AnimationGeneratedVideo | None:
        return self.session.scalar(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.animation_item_id == item_id,
                AnimationGeneratedVideo.selected,
            )
        )
