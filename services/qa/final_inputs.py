"""Authoritative T22 input selection and stale-lineage rejection.

T22 does not re-derive the render. It loads the canonical current T17 render and
its manifest, then proves that the manifest still describes the project's
*current* state. That proof is the whole point of this module, and it happens
before any paid provider request:

* every referenced asset belongs to this project,
* the render was produced from the currently selected script, narration,
  storyboard and animation attempts,
* every selected shot has a passing T20 video-QA result,
* no required shot sits in an active, failed or ``HUMAN_REVIEW_REQUIRED`` T21
  repair state,
* the render contains exactly the selected passing T21 or original outputs,
* the declared narration, storyboard, caption and render hashes match what the
  database actually holds,
* shot ordering and asset references match the render manifest,
* the final asset exists, is nonempty and is readable.

Any failure raises :class:`FinalQALineageError`, which is non-retryable: the
answer is a new render, never a second evaluation of the same stale one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.render_execution.commands import current_render_job
from services.renderer.manifest import bound_manifest_identity
from services.renderer.selection import project_narration_words
from vidgen.contracts.final_editorial import (
    FinalQAFailureCode,
    FinalQAInput,
    FinalSelectedShot,
)
from vidgen.contracts.render import CaptionWord, RenderManifest
from vidgen.contracts.visual_qa import VisualQAOutcome, VisualQATargetType
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.models import Asset, Project, RenderJob
from vidgen.db.narration_models import NarrationRun
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.repair_models import RepairRun
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import BlobStore

#: The T17 terminal status a render must hold before final QA may inspect it.
COMPLETED_RENDER_STATUS = "render_complete"

#: T21 states that mean a shot is not yet eligible for final assembly.
BLOCKING_REPAIR_STATES: dict[str, FinalQAFailureCode] = {
    "REPAIR_PLANNING": FinalQAFailureCode.ACTIVE_REPAIR_RUN,
    "REPAIRING": FinalQAFailureCode.ACTIVE_REPAIR_RUN,
    "ALTERNATE_PROVIDER": FinalQAFailureCode.ACTIVE_REPAIR_RUN,
    "FALLBACK_RENDERING": FinalQAFailureCode.ACTIVE_REPAIR_RUN,
    "REVALIDATING": FinalQAFailureCode.ACTIVE_REPAIR_RUN,
    "HUMAN_REVIEW_REQUIRED": FinalQAFailureCode.UNRESOLVED_REPAIR_REVIEW,
    "REPAIR_FAILED": FinalQAFailureCode.FAILED_REPAIR_RUN,
}


class FinalQALineageError(ValueError):
    """A structural T22 failure raised before any paid provider request."""

    def __init__(
        self, code: FinalQAFailureCode, message: str, *, reference_id: UUID | None = None
    ) -> None:
        super().__init__(message)
        self._code = code
        self.reference_id = reference_id

    @property
    def code(self) -> FinalQAFailureCode:
        return self._code

    @property
    def retryable(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class AuthoritativeFinalInputs:
    """The validated canonical inputs plus the rows T22 needs to evaluate them."""

    inputs: FinalQAInput
    render_job: RenderJob
    manifest: RenderManifest
    manifest_payload: dict[str, Any]
    final_video_asset: Asset
    caption_track: CaptionTrackRecord | None
    #: ``(narration_segment_id, global_start_us, global_end_us)`` per segment.
    narration_intervals: tuple[tuple[UUID, int, int], ...]
    #: The approved T12 word projection, offset onto the global timeline.
    approved_words: tuple[CaptionWord, ...]
    plot_beat_summaries: tuple[str, ...] = ()


class FinalInputSelector:
    """Load and validate the canonical current render for one project."""

    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self._session = session
        self._blob = blob_store
        self._qa = VisualQARepository(session)

    def select(self, project_id: UUID) -> AuthoritativeFinalInputs:
        project = self._session.get(Project, project_id)
        if project is None:
            raise FinalQALineageError(
                FinalQAFailureCode.PROJECT_NOT_FOUND, "project does not exist"
            )
        job = self._render_job(project_id)
        manifest, payload = self._manifest(job, project_id)
        final_asset = self._final_asset(job, manifest, project_id)
        shots = self._shots(project_id, manifest)
        self._upstream_lineage(project_id, manifest)
        caption_track = self._session.scalar(
            select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == job.id)
        )
        caption_ids, caption_hashes = self._captions(manifest, project_id)
        intervals, words = self._narration_projection(manifest)
        inputs = FinalQAInput(
            project_id=project_id,
            render_job_id=job.id,
            render_identity=manifest.render_identity,
            final_video_asset_id=final_asset.id,
            final_video_sha256=final_asset.sha256,
            render_manifest_asset_id=self._require(job.manifest_asset_id, "render manifest"),
            render_manifest_hash=self._manifest_asset_hash(job, project_id),
            approved_script_id=manifest.approved_script_id,
            approved_script_version=manifest.approved_script_version,
            approved_script_hash=manifest.approved_script_hash,
            narration_run_id=manifest.narration_run_id,
            narration_asset_ids=[item.asset_id for item in manifest.narration_assets],
            narration_word_timing_hash=manifest.narration_word_timing_hash,
            narration_duration_us=manifest.narration_duration_us,
            storyboard_run_id=manifest.storyboard_run_id,
            storyboard_hash=manifest.storyboard_hash,
            timing_manifest_hash=manifest.timing_manifest_hash,
            caption_track_id=manifest.caption_track_id,
            caption_identity=manifest.caption_identity,
            caption_asset_ids=caption_ids,
            caption_asset_hashes=caption_hashes,
            final_audio_asset_id=job.premaster_audio_asset_id,
            shots=shots,
            subtitle_mode=manifest.subtitle_mode,
            timeline_duration_us=manifest.shots[-1].global_end_us,
        )
        return AuthoritativeFinalInputs(
            inputs=inputs,
            render_job=job,
            manifest=manifest,
            manifest_payload=payload,
            final_video_asset=final_asset,
            caption_track=caption_track,
            narration_intervals=intervals,
            approved_words=words,
        )

    # --- render -----------------------------------------------------------
    def _render_job(self, project_id: UUID) -> RenderJob:
        # One authoritative lookup for "the project's current render", shared
        # with T17b and T18. T22 keeps its own refusal codes, but it must never
        # evaluate a different render than the one the dashboard offers.
        job = current_render_job(self._session, project_id)
        if job is None or not job.selected:
            raise FinalQALineageError(
                FinalQAFailureCode.RENDER_NOT_SELECTED,
                "project has no selected canonical T17 render",
            )
        if job.status != COMPLETED_RENDER_STATUS or job.final_video_asset_id is None:
            raise FinalQALineageError(
                FinalQAFailureCode.RENDER_INCOMPLETE,
                "the selected T17 render is not complete",
                reference_id=job.id,
            )
        return job

    def _manifest(self, job: RenderJob, project_id: UUID) -> tuple[RenderManifest, dict[str, Any]]:
        asset_id = self._require(job.manifest_asset_id, "render manifest")
        asset = self._asset(asset_id, project_id, FinalQAFailureCode.RENDER_MANIFEST_MISSING)
        try:
            payload = self._blob.read(asset.storage_key)
        except (OSError, KeyError, ValueError) as error:
            raise FinalQALineageError(
                FinalQAFailureCode.RENDER_MANIFEST_MISSING,
                "the render manifest asset is inaccessible",
                reference_id=asset.id,
            ) from error
        try:
            manifest = RenderManifest.model_validate_json(payload)
        except ValueError as error:
            raise FinalQALineageError(
                FinalQAFailureCode.RENDER_MANIFEST_INVALID,
                "the render manifest failed contract validation",
                reference_id=asset.id,
            ) from error
        if manifest.project_id != project_id:
            raise FinalQALineageError(
                FinalQAFailureCode.CROSS_PROJECT_ASSET,
                "the render manifest belongs to another project",
                reference_id=asset.id,
            )
        if bound_manifest_identity(manifest) != manifest.render_identity:
            raise FinalQALineageError(
                FinalQAFailureCode.RENDER_HASH_MISMATCH,
                "the render manifest does not hash to its declared render identity",
                reference_id=asset.id,
            )
        if job.render_identity and job.render_identity != manifest.render_identity:
            raise FinalQALineageError(
                FinalQAFailureCode.STALE_RENDER_LINEAGE,
                "the persisted render job and its manifest disagree on the render identity",
                reference_id=job.id,
            )
        return manifest, dict(json.loads(payload))

    def _manifest_asset_hash(self, job: RenderJob, project_id: UUID) -> str:
        asset = self._asset(
            self._require(job.manifest_asset_id, "render manifest"),
            project_id,
            FinalQAFailureCode.RENDER_MANIFEST_MISSING,
        )
        return asset.sha256

    def _final_asset(self, job: RenderJob, manifest: RenderManifest, project_id: UUID) -> Asset:
        asset = self._asset(
            self._require(job.final_video_asset_id, "final render"),
            project_id,
            FinalQAFailureCode.RENDER_ASSET_MISSING,
        )
        if asset.byte_size <= 0 or not self._blob.exists(asset.storage_key):
            raise FinalQALineageError(
                FinalQAFailureCode.INCOMPLETE_FINAL_ASSET,
                "the final render asset is empty or inaccessible",
                reference_id=asset.id,
            )
        del manifest
        return asset

    # --- shots ------------------------------------------------------------
    def _shots(self, project_id: UUID, manifest: RenderManifest) -> list[FinalSelectedShot]:
        shot_rows = {
            row.id: row
            for row in self._session.scalars(
                select(StoryboardShotRecord).where(
                    StoryboardShotRecord.storyboard_run_id == manifest.storyboard_run_id
                )
            )
        }
        selected: list[FinalSelectedShot] = []
        for entry in manifest.shots:
            shot = shot_rows.get(entry.shot_id)
            if shot is None:
                raise FinalQALineageError(
                    FinalQAFailureCode.SHOT_ORDER_MISMATCH,
                    "the render manifest references a shot outside the selected storyboard",
                    reference_id=entry.shot_id,
                )
            if shot.global_sequence != entry.sequence:
                raise FinalQALineageError(
                    FinalQAFailureCode.SHOT_ORDER_MISMATCH,
                    "shot ordering differs from the render manifest",
                    reference_id=entry.shot_id,
                )
            video = self._session.scalar(
                select(AnimationGeneratedVideo).where(
                    AnimationGeneratedVideo.shot_id == entry.shot_id,
                    AnimationGeneratedVideo.selected.is_(True),
                )
            )
            if video is None:
                raise FinalQALineageError(
                    FinalQAFailureCode.MISSING_UPSTREAM_OUTPUT,
                    "a rendered shot no longer has a selected canonical clip",
                    reference_id=entry.shot_id,
                )
            # A newer selected animation invalidates the render outright: the
            # bytes in the delivery are no longer the bytes the project holds.
            if (
                video.canonical_asset_id != entry.video.asset_id
                or video.sha256 != entry.video.sha256
            ):
                raise FinalQALineageError(
                    FinalQAFailureCode.STALE_SHOT_SELECTION,
                    "the render was produced from a superseded shot selection",
                    reference_id=entry.shot_id,
                )
            asset = self._session.get(Asset, entry.video.asset_id)
            if asset is None or asset.project_id != project_id:
                raise FinalQALineageError(
                    FinalQAFailureCode.CROSS_PROJECT_ASSET,
                    "a rendered shot clip belongs to another project",
                    reference_id=entry.shot_id,
                )
            if asset.sha256 != entry.video.sha256:
                raise FinalQALineageError(
                    FinalQAFailureCode.ASSET_REFERENCE_MISMATCH,
                    "a rendered shot clip hash differs from the render manifest",
                    reference_id=entry.shot_id,
                )
            qa_run_id, qa_result_id = self._require_video_qa(entry.shot_id)
            repair_run_id, repair_attempt_id = self._require_repair_state(
                entry.shot_id, entry.video.asset_id
            )
            selected.append(
                FinalSelectedShot(
                    shot_id=entry.shot_id,
                    sequence=entry.sequence,
                    video_asset_id=entry.video.asset_id,
                    video_sha256=entry.video.sha256,
                    global_start_us=entry.global_start_us,
                    global_end_us=entry.global_end_us,
                    shot_workflow_identity=entry.shot_workflow_identity,
                    video_qa_run_id=qa_run_id,
                    video_qa_result_id=qa_result_id,
                    repair_run_id=repair_run_id,
                    selected_repair_attempt_id=repair_attempt_id,
                )
            )
        return selected

    def _require_video_qa(self, shot_id: UUID) -> tuple[UUID, UUID]:
        run = self._qa.canonical_run(shot_id, VisualQATargetType.VIDEO)
        if run is None:
            raise FinalQALineageError(
                FinalQAFailureCode.MISSING_VIDEO_QA_RESULT,
                "a selected shot has no passing T20 video-QA result",
                reference_id=shot_id,
            )
        allowed, _reason = self._qa.gate(shot_id, VisualQATargetType.VIDEO)
        if not allowed or run.final_outcome not in {
            VisualQAOutcome.PASS.value,
            VisualQAOutcome.REVIEW.value,
        }:
            raise FinalQALineageError(
                FinalQAFailureCode.FAILING_VIDEO_QA_RESULT,
                "a selected shot's T20 video QA does not permit final assembly",
                reference_id=shot_id,
            )
        result = self._qa.canonical_result(run.id)
        if result is None:
            raise FinalQALineageError(
                FinalQAFailureCode.MISSING_VIDEO_QA_RESULT,
                "a selected shot's T20 video-QA run has no canonical result",
                reference_id=shot_id,
            )
        return run.id, result.id

    def _require_repair_state(
        self, shot_id: UUID, rendered_asset_id: UUID
    ) -> tuple[UUID | None, UUID | None]:
        runs = list(self._session.scalars(select(RepairRun).where(RepairRun.shot_id == shot_id)))
        if not runs:
            return None, None
        for run in runs:
            code = BLOCKING_REPAIR_STATES.get(run.state)
            if code is not None:
                raise FinalQALineageError(
                    code,
                    f"shot repair run is {run.state} and is not eligible for final assembly",
                    reference_id=run.id,
                )
        locked = [run for run in runs if run.state == "LOCKED"]
        if not locked:
            return None, None
        latest = max(locked, key=lambda run: run.updated_at)
        if latest.selected_asset_id != rendered_asset_id:
            raise FinalQALineageError(
                FinalQAFailureCode.STALE_SHOT_SELECTION,
                "the render does not contain the selected passing T21 attempt for this shot",
                reference_id=latest.id,
            )
        return latest.id, latest.selected_attempt_id

    # --- upstream lineage ---------------------------------------------------
    def _upstream_lineage(self, project_id: UUID, manifest: RenderManifest) -> None:
        script = self._session.scalar(
            select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
        )
        if script is None or script.id != manifest.approved_script_id:
            raise FinalQALineageError(
                FinalQAFailureCode.STALE_RENDER_LINEAGE,
                "the render was produced from a superseded approved script",
                reference_id=manifest.approved_script_id,
            )
        if script.version != manifest.approved_script_version:
            raise FinalQALineageError(
                FinalQAFailureCode.STALE_RENDER_LINEAGE,
                "the render was produced from a superseded script version",
                reference_id=script.id,
            )
        narration = self._session.scalar(
            select(NarrationRun).where(
                NarrationRun.project_id == project_id, NarrationRun.selected.is_(True)
            )
        )
        if narration is None or narration.id != manifest.narration_run_id:
            raise FinalQALineageError(
                FinalQAFailureCode.NARRATION_HASH_MISMATCH,
                "the render was produced from a superseded narration run",
                reference_id=manifest.narration_run_id,
            )
        storyboard = self._session.scalar(
            select(StoryboardRun).where(
                StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
            )
        )
        if storyboard is None or storyboard.id != manifest.storyboard_run_id:
            raise FinalQALineageError(
                FinalQAFailureCode.STORYBOARD_HASH_MISMATCH,
                "the render was produced from a superseded storyboard run",
                reference_id=manifest.storyboard_run_id,
            )

    # --- captions and narration --------------------------------------------
    def _captions(self, manifest: RenderManifest, project_id: UUID) -> tuple[list[UUID], list[str]]:
        ids: list[UUID] = []
        hashes: list[str] = []
        for reference in manifest.caption_assets:
            asset = self._asset(
                reference.asset_id, project_id, FinalQAFailureCode.CAPTION_HASH_MISMATCH
            )
            if asset.sha256 != reference.sha256:
                raise FinalQALineageError(
                    FinalQAFailureCode.CAPTION_HASH_MISMATCH,
                    "a delivered caption asset hash differs from the render manifest",
                    reference_id=asset.id,
                )
            ids.append(asset.id)
            hashes.append(asset.sha256)
        return ids, hashes

    def _narration_projection(
        self, manifest: RenderManifest
    ) -> tuple[tuple[tuple[UUID, int, int], ...], tuple[CaptionWord, ...]]:
        """Project approved T12 segments and word timings onto the global timeline.

        T17b builds the deliverable caption track from the same projection, so
        this stays a shared definition rather than a second interpretation of
        the same rows: T22's independent reconstruction is a check on the
        renderer, not on the projection.
        """
        return project_narration_words(
            self._session,
            storyboard_run_id=manifest.storyboard_run_id,
            narration_run_id=manifest.narration_run_id,
        )

    # --- helpers ------------------------------------------------------------
    def _asset(self, asset_id: UUID, project_id: UUID, code: FinalQAFailureCode) -> Asset:
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise FinalQALineageError(code, "a required asset is missing", reference_id=asset_id)
        if asset.project_id is not None and asset.project_id != project_id:
            raise FinalQALineageError(
                FinalQAFailureCode.CROSS_PROJECT_ASSET,
                "a required asset belongs to another project",
                reference_id=asset_id,
            )
        return asset

    @staticmethod
    def _require(value: UUID | None, label: str) -> UUID:
        if value is None:
            raise FinalQALineageError(
                FinalQAFailureCode.MISSING_UPSTREAM_OUTPUT, f"the {label} asset is missing"
            )
        return value
