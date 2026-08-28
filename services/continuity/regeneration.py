"""Apply an approved bundle with exact T14/T15/T16/T17 invalidation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.continuity import ReferenceInvalidation, ShotReferenceBundle
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.models import RenderJob
from vidgen.db.storyboard_models import StoryboardShotRecord

RegenerateShot = Callable[[UUID, str, str], None]


class ContinuityRegenerator:
    """Stale and requeue only shots whose immutable bundle changed.

    Asset rows, provider attempts, and ledger entries are deliberately untouched.
    The injected callback is the existing T16 command boundary, keeping Temporal
    and provider calls outside this deterministic persistence service.
    """

    def __init__(self, session: Session, regenerate_shot: RegenerateShot) -> None:
        self.session = session
        self.regenerate_shot = regenerate_shot

    def apply(
        self,
        *,
        project_id: UUID,
        bundles: Sequence[ShotReferenceBundle],
        idempotency_key: str,
    ) -> ReferenceInvalidation:
        affected_shots = {bundle.shot_id: bundle for bundle in bundles}
        all_shots = set(
            self.session.scalars(
                select(StoryboardShotRecord.id)
                .join(
                    ImageGenerationRun,
                    ImageGenerationRun.storyboard_id == StoryboardShotRecord.storyboard_run_id,
                )
                .where(ImageGenerationRun.project_id == project_id)
            )
        )
        keyframes = list(
            self.session.scalars(
                select(GeneratedKeyframeImage).where(
                    GeneratedKeyframeImage.project_id == project_id,
                    GeneratedKeyframeImage.shot_id.in_(affected_shots),
                    GeneratedKeyframeImage.selected.is_(True),
                )
            )
        )
        for image in keyframes:
            image.selected = False
            item = self.session.get(ImageGenerationItem, image.item_id)
            if item is not None:
                item.status = "stale_continuity_reference"
                item.selected_generated_image_id = None

        videos = list(
            self.session.scalars(
                select(AnimationGeneratedVideo).where(
                    AnimationGeneratedVideo.project_id == project_id,
                    AnimationGeneratedVideo.shot_id.in_(affected_shots),
                    AnimationGeneratedVideo.selected.is_(True),
                )
            )
        )
        for video in videos:
            video.selected = False
            animation_item = self.session.get(AnimationItem, video.animation_item_id)
            if animation_item is not None:
                animation_item.status = "stale_continuity_reference"
                animation_item.selected_generated_video_id = None

        renders = list(
            self.session.scalars(
                select(RenderJob).where(
                    RenderJob.project_id == project_id,
                    RenderJob.status == "render_complete",
                )
            )
        )
        for render in renders:
            render.status = "stale"

        # Preserve sibling runs and all historical media. Affected run summaries
        # become incomplete, but no unrelated item is unlocked or resubmitted.
        self._refresh_run_statuses(project_id)
        for shot_id, bundle in sorted(affected_shots.items(), key=lambda item: str(item[0])):
            self.regenerate_shot(
                shot_id,
                bundle.bundle_hash,
                f"{idempotency_key}:shot:{shot_id}:{bundle.bundle_hash}",
            )
        self.session.flush()
        return ReferenceInvalidation(
            project_id=project_id,
            affected_shot_ids=sorted(affected_shots, key=str),
            preserved_shot_ids=sorted(all_shots - set(affected_shots), key=str),
            stale_keyframe_ids=sorted((item.id for item in keyframes), key=str),
            stale_video_ids=sorted((item.id for item in videos), key=str),
            stale_render_ids=sorted((item.id for item in renders), key=str),
        )

    def _refresh_run_statuses(self, project_id: UUID) -> None:
        for run in self.session.scalars(
            select(ImageGenerationRun).where(ImageGenerationRun.project_id == project_id)
        ):
            if self.session.scalar(
                select(ImageGenerationItem.id).where(
                    ImageGenerationItem.run_id == run.id,
                    ImageGenerationItem.status == "stale_continuity_reference",
                )
            ):
                run.status = "keyframes_stale"
        for animation_run in self.session.scalars(
            select(AnimationRun).where(AnimationRun.project_id == project_id)
        ):
            if self.session.scalar(
                select(AnimationItem.id).where(
                    AnimationItem.run_id == animation_run.id,
                    AnimationItem.status == "stale_continuity_reference",
                )
            ):
                animation_run.status = "videos_stale"
