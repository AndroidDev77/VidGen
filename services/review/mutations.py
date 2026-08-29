"""Transactional review-UI mutations.

Each method assumes the caller has already resolved an owner-scoped project and
enforced ``If-Match`` and ``Idempotency-Key``. The service applies exactly one
domain change, records the downstream invalidation it caused, bumps the affected
row versions, and appends one bounded project event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.render_execution.inputs import resolve_render_inputs
from services.renderer.selection import RenderLineageError
from services.review.invalidation import (
    InvalidationRecorder,
    script_invalidation_set,
    shot_invalidation_set,
    transcript_invalidation_set,
)
from services.review.shot_identity import (
    current_workflow_id,
    regenerated_workflow_id,
    shot_workflow_identity,
)
from services.script.canonicalize import compute_segment_content_hash
from vidgen.contracts.render_execution import RenderExecutionStatus
from vidgen.contracts.review import (
    ApiErrorCode,
    InvalidationSet,
    PipelineStage,
    ShotRegenerationResult,
)
from vidgen.contracts.shot_workflow import ShotWorkflowCommand, ShotWorkflowCommandResult
from vidgen.db.animation_models import AnimationGeneratedVideo
from vidgen.db.models import Project, RenderJob
from vidgen.db.review_models import RenderApproval
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.transcription_models import Transcript, TranscriptSegmentRecord
from vidgen.review.errors import ReviewError, conflict, not_found, validation_failed
from vidgen.review.events import ProjectEventService
from vidgen.review.projections import current_render, render_is_stale, selected_storyboard
from vidgen.review.versions import RowVersionService
from vidgen.review.workflow_control import WorkflowController


@dataclass(frozen=True, slots=True)
class ScriptEditOutcome:
    script: Script
    segment: ScriptSegment
    invalidation: InvalidationSet
    created_version: bool


@dataclass(frozen=True, slots=True)
class ShotRegenerationOutcome:
    result: ShotRegenerationResult


class ReviewMutationService:
    def __init__(
        self,
        session: Session,
        owner_subject: str,
        versions: RowVersionService,
        events: ProjectEventService,
        controller: WorkflowController,
        t14_configuration_identity: str,
        t15_capability_profile_identity: str,
    ) -> None:
        self._session = session
        self._owner = owner_subject
        self._versions = versions
        self._events = events
        self._controller = controller
        self._t14_identity = t14_configuration_identity
        self._t15_identity = t15_capability_profile_identity
        self._invalidations = InvalidationRecorder(session)

    def _shot_workflow_id(self, shot: StoryboardShotRecord) -> str:
        """The Temporal ID of the child that currently owns this shot."""
        run = self._session.get(StoryboardRun, shot.storyboard_run_id)
        if run is None:
            raise not_found("shot workflow")
        return current_workflow_id(
            shot_workflow_identity(
                self._session,
                run,
                shot,
                t14_configuration_identity=self._t14_identity,
                t15_capability_profile_identity=self._t15_identity,
            )
        )

    # ------------------------------------------------------------------
    # Transcript
    # ------------------------------------------------------------------

    def edit_transcript_segment(
        self,
        project: Project,
        transcript: Transcript,
        segment: TranscriptSegmentRecord,
        *,
        text: str | None,
        speaker_label: str | None,
        confirm_invalidation: bool,
    ) -> tuple[TranscriptSegmentRecord, InvalidationSet, int]:
        """Edit one segment, preserving its original provider provenance."""
        invalidation = transcript_invalidation_set(self._session, project.id)
        if invalidation.requires_confirmation and not confirm_invalidation:
            raise ReviewError(
                409,
                conflict(
                    ApiErrorCode.VERSION_CONFLICT,
                    "This transcript edit invalidates downstream work. Confirm to continue.",
                ).error,
            )
        provenance = dict(segment.provenance or {})
        if "original" not in provenance:
            provenance["original"] = {
                "text": segment.text,
                "speaker_label": segment.speaker_label,
            }
        provenance["edited_by_owner"] = True
        provenance["edited_at"] = datetime.now(UTC).isoformat()
        if text is not None:
            segment.text = text
        if speaker_label is not None:
            segment.speaker_label = speaker_label
        segment.provenance = provenance
        self._session.flush()
        self._versions.bump(project.id, "transcript_segment", segment.id)
        transcript_version = self._versions.bump(project.id, "transcript", transcript.id)
        self._invalidations.record(project.id, "transcript", transcript.id, invalidation)
        self._events.append(
            project.id,
            event_type="transcript_edited",
            status="edited",
            stage=PipelineStage.TRANSCRIPT_ACQUISITION,
        )
        return segment, invalidation, transcript_version

    # ------------------------------------------------------------------
    # Script
    # ------------------------------------------------------------------

    def edit_script_segment(
        self,
        project: Project,
        script: Script,
        segment: ScriptSegment,
        *,
        text: str | None,
        visual_gag: str | None,
        confirm_invalidation: bool,
    ) -> ScriptEditOutcome:
        """Edit one script segment, revising an immutable version rather than mutating it."""
        new_text = text if text is not None else segment.text
        new_gag = visual_gag if visual_gag is not None else segment.visual_gag
        if not new_text.strip():
            raise validation_failed("A script segment cannot be empty.")
        material = new_text != segment.text or new_gag != segment.visual_gag
        invalidation = (
            script_invalidation_set(self._session, project.id)
            if material
            else InvalidationSet(entries=[], requires_confirmation=False)
        )
        if invalidation.requires_confirmation and not confirm_invalidation:
            raise conflict(
                ApiErrorCode.VERSION_CONFLICT,
                "This script change invalidates downstream work. Confirm to continue.",
            )
        target_script = script
        target_segment = segment
        created_version = False
        if material and script.status in {"approved", "selected", "final"}:
            target_script, target_segment = self._revise_script(script, segment)
            created_version = True
        target_segment.text = new_text
        target_segment.visual_gag = new_gag
        target_segment.content_hash = compute_segment_content_hash(
            text=new_text,
            segment_type=target_segment.segment_type,
            speaker_kind=target_segment.speaker_kind,
            speaker_character_id=target_segment.speaker_character_id,
            anonymous_speaker_label=target_segment.anonymous_speaker_label,
            joke_annotations=list(target_segment.joke_annotations or []),
            visual_gag=new_gag,
            voice_direction=target_segment.voice_direction,
        )
        target_script.actual_word_count = sum(
            len(row.text.split())
            for row in self._session.scalars(
                select(ScriptSegment).where(ScriptSegment.script_id == target_script.id)
            ).all()
        )
        self._session.flush()
        self._versions.bump(project.id, "script_segment", target_segment.id)
        self._versions.bump(project.id, "script", target_script.id)
        if material:
            self._invalidations.record(project.id, "script", target_script.id, invalidation)
        self._events.append(
            project.id,
            event_type="script_edited",
            status="edited",
            stage=PipelineStage.SCRIPT_GENERATION,
        )
        return ScriptEditOutcome(
            script=target_script,
            segment=target_segment,
            invalidation=invalidation,
            created_version=created_version,
        )

    def _revise_script(
        self, script: Script, segment: ScriptSegment
    ) -> tuple[Script, ScriptSegment]:
        """Copy an approved version into a new selected revision, preserving the original."""
        next_version = (
            max(
                row.version
                for row in self._session.scalars(
                    select(Script).where(Script.project_id == script.project_id)
                ).all()
            )
            + 1
        )
        revision = Script(
            project_id=script.project_id,
            generation_run_id=script.generation_run_id,
            episode_analysis_id=script.episode_analysis_id,
            compressed_plot_plan_id=script.compressed_plot_plan_id,
            parent_script_id=script.id,
            version=next_version,
            status="draft",
            target_word_count=script.target_word_count,
            actual_word_count=script.actual_word_count,
            target_duration_ms=script.target_duration_ms,
            humor_intensity=script.humor_intensity,
            canonical_script_asset_id=script.canonical_script_asset_id,
            prompt_version=script.prompt_version,
            rubric_version=script.rubric_version,
            review_scores=script.review_scores,
            selected=False,
        )
        self._session.add(revision)
        self._session.flush()
        copied: ScriptSegment | None = None
        for row in self._session.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.script_id == script.id)
            .order_by(ScriptSegment.sequence)
        ).all():
            clone = ScriptSegment(
                script_id=revision.id,
                sequence=row.sequence,
                stable_segment_id=row.stable_segment_id,
                segment_type=row.segment_type,
                speaker_kind=row.speaker_kind,
                speaker_character_id=row.speaker_character_id,
                anonymous_speaker_label=row.anonymous_speaker_label,
                text=row.text,
                content_hash=row.content_hash,
                plot_beat_ids=list(row.plot_beat_ids or []),
                source_scene_ids=list(row.source_scene_ids or []),
                joke_annotations=list(row.joke_annotations or []),
                visual_gag=row.visual_gag,
                estimated_duration_ms=row.estimated_duration_ms,
                voice_direction=row.voice_direction,
                locked=False,
            )
            self._session.add(clone)
            if row.id == segment.id:
                copied = clone
        self._session.flush()
        if copied is None:
            raise not_found("script segment")
        # Flush the deselect before the select: the project has a unique partial
        # index allowing exactly one selected script.
        script.selected = False
        self._session.flush()
        revision.selected = True
        self._session.flush()
        return revision, copied

    def select_script(self, project: Project, script: Script) -> Script:
        if script.project_id != project.id:
            raise not_found("script")
        for row in self._session.scalars(
            select(Script).where(Script.project_id == project.id, Script.selected.is_(True))
        ).all():
            row.selected = False
        self._session.flush()
        script.selected = True
        self._session.flush()
        self._versions.bump(project.id, "script", script.id)
        self._events.append(
            project.id,
            event_type="script_selected",
            status="selected",
            stage=PipelineStage.SCRIPT_GENERATION,
        )
        return script

    # ------------------------------------------------------------------
    # Shots
    # ------------------------------------------------------------------

    def regenerate_shot(
        self,
        project: Project,
        shot: StoryboardShotRecord,
        *,
        idempotency_key: str,
        row_version: int,
        confirm_invalidation: bool,
    ) -> ShotRegenerationOutcome:
        """Start one new shot child workflow without touching sibling shots."""
        invalidation = shot_invalidation_set(self._session, project.id, shot)
        if invalidation.requires_confirmation and not confirm_invalidation:
            raise conflict(
                ApiErrorCode.VERSION_CONFLICT,
                "Regenerating this shot marks the current verified render stale. "
                "Confirm to continue.",
            )
        run = self._session.get(StoryboardRun, shot.storyboard_run_id)
        if run is None:
            raise not_found("shot workflow")
        identity = shot_workflow_identity(
            self._session,
            run,
            shot,
            t14_configuration_identity=self._t14_identity,
            t15_capability_profile_identity=self._t15_identity,
        )
        previous_identity = identity.identity_hash
        # A new material regeneration identity: the same shot deliberately gets a
        # different T16 child rather than overwriting the locked one.
        new_identity = hashlib.sha256(
            f"t18-regenerate:{previous_identity}:{idempotency_key}:{row_version}".encode()
        ).hexdigest()
        command = ShotWorkflowCommand(
            command_id=f"t18-regenerate-{idempotency_key}"[:128],
            project_id=project.id,
            storyboard_shot_id=shot.stable_shot_id,
            command="regenerate",
            new_shot_input_hash=new_identity,
        )
        # The command goes to the child that currently owns the shot; the ID
        # returned is the one the replacement child will take.
        result = self._controller.send_shot_command(current_workflow_id(identity), command)
        child_workflow_id = regenerated_workflow_id(shot.stable_shot_id, new_identity)
        if not result.accepted:
            raise conflict(
                ApiErrorCode.SHOT_NOT_RETRYABLE,
                f"The shot workflow rejected the regeneration command ({result.code}).",
            )
        preserved = [
            row.id
            for row in self._session.scalars(
                select(AnimationGeneratedVideo).where(AnimationGeneratedVideo.shot_id == shot.id)
            ).all()
        ]
        self._invalidations.record(project.id, "shot", shot.id, invalidation)
        self._mark_render_stale(project)
        new_row_version = self._versions.bump(project.id, "shot", shot.id)
        self._events.append(
            project.id,
            event_type="shot_regeneration_started",
            status="running",
            stage=PipelineStage.SHOT_ORCHESTRATION,
        )
        return ShotRegenerationOutcome(
            result=ShotRegenerationResult(
                shot_id=shot.id,
                child_workflow_id=child_workflow_id,
                new_identity_hash=new_identity,
                previous_identity_hash=previous_identity,
                preserved_attempt_ids=preserved,
                invalidation=invalidation,
                row_version=new_row_version,
            )
        )

    def retry_shot(
        self, project: Project, shot: StoryboardShotRecord, *, idempotency_key: str
    ) -> ShotWorkflowCommandResult:
        command = ShotWorkflowCommand(
            command_id=f"t18-retry-{idempotency_key}"[:128],
            project_id=project.id,
            storyboard_shot_id=shot.stable_shot_id,
            command="retry",
        )
        result = self._controller.send_shot_command(self._shot_workflow_id(shot), command)
        if not result.accepted:
            raise conflict(
                ApiErrorCode.SHOT_NOT_RETRYABLE,
                "This shot is not in a retryable failed state.",
            )
        self._events.append(
            project.id,
            event_type="shot_retry_requested",
            status="running",
            stage=PipelineStage.SHOT_ORCHESTRATION,
        )
        return result

    def cancel_shot(
        self, project: Project, shot: StoryboardShotRecord, *, idempotency_key: str
    ) -> ShotWorkflowCommandResult:
        command = ShotWorkflowCommand(
            command_id=f"t18-cancel-{idempotency_key}"[:128],
            project_id=project.id,
            storyboard_shot_id=shot.stable_shot_id,
            command="cancel",
        )
        result = self._controller.send_shot_command(self._shot_workflow_id(shot), command)
        self._events.append(
            project.id,
            event_type="shot_cancel_requested",
            status="cancelled",
            stage=PipelineStage.SHOT_ORCHESTRATION,
        )
        return result

    def select_shot_attempt(
        self, project: Project, shot: StoryboardShotRecord, attempt_id: UUID
    ) -> AnimationGeneratedVideo:
        """Select a completed video attempt for one shot; siblings are untouched."""
        attempt = self._session.get(AnimationGeneratedVideo, attempt_id)
        if attempt is None or attempt.shot_id != shot.id:
            raise not_found("shot attempt")
        for row in self._session.scalars(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).all():
            row.selected = False
        self._session.flush()
        attempt.selected = True
        self._session.flush()
        self._mark_render_stale(project)
        self._versions.bump(project.id, "shot", shot.id)
        self._events.append(
            project.id,
            event_type="shot_attempt_selected",
            status="selected",
            stage=PipelineStage.SHOT_ORCHESTRATION,
        )
        return attempt

    # ------------------------------------------------------------------
    # Render and approval
    # ------------------------------------------------------------------

    def start_render(self, project: Project, *, idempotency_key: str) -> RenderJob:
        """Queue one render job. T17b executes it out of band.

        The queued row is executable as-is: the canonical queue command resolves
        the project's authoritative inputs and stamps the render's input
        identity, so a worker can claim this job and render it without anyone
        constructing a manifest by hand.

        When the project cannot currently produce a render - an unapproved
        script, a shot still awaiting T20, an active T21 repair - the job is
        still queued, with the structured refusal recorded on the row. The
        review UI needs to show *why* a render is blocked, and a 500 or a
        silently missing row shows nothing.
        """
        existing = self._session.scalar(
            select(RenderJob).where(
                RenderJob.project_id == project.id,
                RenderJob.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
        storyboard = selected_storyboard(self._session, project.id)
        attempt = (
            len(
                self._session.scalars(
                    select(RenderJob.id).where(RenderJob.project_id == project.id)
                ).all()
            )
            + 1
        )
        job = RenderJob(
            project_id=project.id,
            status=RenderExecutionStatus.QUEUED.value,
            attempt=attempt,
            idempotency_key=idempotency_key,
            storyboard_run_id=storyboard.id,
            script_id=storyboard.script_id,
            script_version=storyboard.script_version,
            narration_run_id=storyboard.narration_run_id,
            error={},
        )
        self._session.add(job)
        self._session.flush()
        self._stamp_input_identity(job)
        self._events.append(
            project.id,
            event_type="render_started",
            status=job.status,
            stage=PipelineStage.RENDERING,
            payload={"render_status": job.status},
        )
        return job

    def _stamp_input_identity(self, job: RenderJob) -> None:
        """Record the render's authoritative input identity, or why there isn't one.

        The executor re-resolves and re-hashes the inputs when it claims the job,
        and refuses to render when the identity has moved. Stamping it here means
        a change between queueing and execution is caught rather than absorbed.
        """
        try:
            resolved = resolve_render_inputs(self._session, job=job)
        except RenderLineageError as error:
            job.error = {
                "code": error.code,
                "message": str(error)[:1024],
                "retryable": error.retryable,
                "warnings": [],
            }
            job.error_code = error.code
            self._session.flush()
            return
        job.input_hash = resolved.input_hash
        job.input_selection = resolved.contract.model_dump(mode="json")
        job.expected_duration_us = resolved.total_duration_us
        job.error_code = None
        job.error = {}
        self._session.flush()

    def approve_render(
        self, project: Project, render: RenderJob, lineage_hash: str
    ) -> RenderApproval:
        """Record a versioned approval of a complete, verified, current render."""
        if render.status != "render_complete":
            raise conflict(
                ApiErrorCode.RENDER_NOT_VERIFIED,
                "Only a completed render can be approved.",
            )
        if render.verification_report_asset_id is None:
            raise conflict(
                ApiErrorCode.RENDER_NOT_VERIFIED,
                "This render has no successful verification report.",
            )
        if render_is_stale(self._session, project.id, render):
            raise conflict(
                ApiErrorCode.RENDER_STALE,
                "This render is stale: its upstream lineage changed after it was produced.",
            )
        existing = self._session.scalar(
            select(RenderApproval).where(
                RenderApproval.render_job_id == render.id,
                RenderApproval.lineage_hash == lineage_hash,
            )
        )
        if existing is not None:
            return existing
        approval = RenderApproval(
            id=uuid4(),
            project_id=project.id,
            render_job_id=render.id,
            approved_by=self._owner,
            lineage_hash=lineage_hash,
            approved_at=datetime.now(UTC),
        )
        self._session.add(approval)
        self._session.flush()
        self._versions.bump(project.id, "render", render.id)
        self._events.append(
            project.id,
            event_type="render_approved",
            status="approved",
            stage=PipelineStage.REVIEW,
            payload={"render_status": render.status},
        )
        return approval

    def _mark_render_stale(self, project: Project) -> None:
        """Mark the current verified render historical rather than deleting it."""
        render = current_render(self._session, project.id)
        if render is None or render.status != "render_complete":
            return
        self._events.append(
            project.id,
            event_type="render_marked_stale",
            status="stale",
            stage=PipelineStage.RENDERING,
            payload={"render_status": "stale"},
        )
