"""Read projections for the T18 review UI.

Every function here takes an already owner-verified project and returns a
bounded contract from :mod:`vidgen.contracts.review`. Selection follows the
existing domain rules: the selected transcript, the selected script, the
selected storyboard run, the T16 locked shot outputs, and the selected T17
render.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import ControlCommandStatus, ControlCommandType
from vidgen.contracts.review import (
    PIPELINE_STAGE_ORDER,
    PipelineStage,
    ProjectRunState,
    ProjectSummaryProjection,
    RenderApprovalProjection,
    RenderProjection,
    ScriptProjection,
    ScriptSegmentProjection,
    ScriptSummaryProjection,
    ShotAttemptProjection,
    ShotCommandProjection,
    ShotDetailProjection,
    ShotStatusProjection,
    StageState,
    StageTimelineEntry,
    StoryboardProjection,
    StoryboardShotProjection,
    TranscriptProjection,
    TranscriptSegmentProjection,
    WorkflowStatusProjection,
)
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, RunwayTask
from vidgen.db.control_command_models import ControlCommandRecord
from vidgen.db.cost_models import (
    CostLedgerEntry,
    PipelineFailureEvent,
    ProjectBudget,
    ProviderAttempt,
)
from vidgen.db.image_generation_models import GeneratedKeyframeImage, ImageGenerationItem
from vidgen.db.models import Project, RenderJob
from vidgen.db.narration_models import NarrationSegment
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.review_models import DownstreamInvalidation, RenderApproval
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.transcription_models import Transcript, TranscriptSegmentRecord
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.errors import not_found
from vidgen.review.lineage import render_lineage_hash
from vidgen.review.versions import RowVersionService
from vidgen.review.workflow_control import LIVE_RUN_STATUSES

# The parent workflow's stage names, mapped onto the timeline the UI renders.
WORKFLOW_STAGE_ALIASES: dict[str, PipelineStage] = {
    "upload": PipelineStage.UPLOAD,
    "media_processing": PipelineStage.MEDIA_PROCESSING,
    "transcript_acquisition": PipelineStage.TRANSCRIPT_ACQUISITION,
    "evidence": PipelineStage.EVIDENCE,
    "episode_analysis": PipelineStage.EPISODE_ANALYSIS,
    "script_generation": PipelineStage.SCRIPT_GENERATION,
    "narration": PipelineStage.NARRATION,
    "storyboard": PipelineStage.STORYBOARD,
    "keyframes": PipelineStage.KEYFRAMES,
    # The T14 stage names itself "image_generation" in the worker's handler
    # table and "keyframes" on the timeline. A failure recorded under either
    # spelling has to reach the same retryable stage.
    "image_generation": PipelineStage.KEYFRAMES,
    "animation": PipelineStage.ANIMATION,
    "shot_generation": PipelineStage.SHOT_ORCHESTRATION,
    "shot_generation_running": PipelineStage.SHOT_ORCHESTRATION,
    "shot_generation_complete": PipelineStage.SHOT_ORCHESTRATION,
    "captions": PipelineStage.CAPTIONS,
    "rendering": PipelineStage.RENDERING,
    # The T17b render stage's workflow statuses. They are the durable
    # render-job statuses, so the timeline needs no translation table of its own.
    "render": PipelineStage.RENDERING,
    "render_queued": PipelineStage.RENDERING,
    "render_claiming": PipelineStage.RENDERING,
    "render_preparing": PipelineStage.RENDERING,
    "render_manifest_ready": PipelineStage.RENDERING,
    "render_rendering": PipelineStage.RENDERING,
    "render_verifying": PipelineStage.RENDERING,
    "render_persisting": PipelineStage.RENDERING,
    "render_complete": PipelineStage.RENDERING,
    "render_failed": PipelineStage.RENDERING,
    "render_cancelled": PipelineStage.RENDERING,
    "review": PipelineStage.REVIEW,
}


#: Workflow statuses that mean the run stopped on a failure rather than on a
#: human decision. Reconciliation writes the bare ``"failed"``; the rest are the
#: stage-specific statuses a workflow records for itself on the way out.
_FAILED_WORKFLOW_STATUSES: frozenset[str] = frozenset(
    {
        "failed",
        "script_generation_failed",
        "shot_generation_failed",
        "render_failed",
        "FINAL_QA_FAILED",
        "references_failed",
    }
)

#: Project statuses that name the pipeline's end without naming a stage. A
#: project that got there by finishing T22 is done even though ``completed`` and
#: ``final_qa_passed`` have no entry in :data:`WORKFLOW_STAGE_ALIASES`.
_FINISHED_PROJECT_STATUSES: frozenset[str] = frozenset({"completed", "final_qa_passed"})


def project_run_state(run: ProjectWorkflowRun | None, *, project_status: str) -> ProjectRunState:
    """What the recorded execution proves about a project right now.

    Only the run row and the project's own status are read, so this stays cheap
    enough for the list endpoint to compute per project: no round trip to the
    cluster, and nothing claimed that was not written down. The cost is that a
    run row nobody has reconciled still reads as ``running``; the dashboard's
    workflow endpoint is what settles that, and it settles it for this too.
    """
    if run is None:
        return ProjectRunState.NOT_STARTED
    if run.status == "cancelled":
        return ProjectRunState.CANCELLED
    if run.status in _FAILED_WORKFLOW_STATUSES:
        return ProjectRunState.FAILED
    if run.status in LIVE_RUN_STATUSES:
        return ProjectRunState.RUNNING
    # The execution is over. It ending is not the same as the recap being
    # finished: the parent workflow completes at every human pause, and only a
    # project that reached the last pipeline stage has actually arrived.
    if (
        project_status in _FINISHED_PROJECT_STATUSES
        or WORKFLOW_STAGE_ALIASES.get(project_status) is PIPELINE_STAGE_ORDER[-1]
    ):
        return ProjectRunState.COMPLETED
    return ProjectRunState.STOPPED


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Owner-scoped resolution
# --------------------------------------------------------------------------


def resolve_project(session: Session, project_id: UUID, owner_subject: str) -> Project:
    """Return the project only when it exists and belongs to this principal."""
    project = session.get(Project, project_id)
    if project is None or project.owner_subject != owner_subject:
        raise not_found("project")
    return project


def selected_transcript(session: Session, project_id: UUID) -> Transcript:
    transcript = session.scalar(
        select(Transcript).where(Transcript.project_id == project_id, Transcript.selected.is_(True))
    )
    if transcript is None:
        raise not_found("transcript")
    return transcript


def selected_script(session: Session, project_id: UUID) -> Script:
    script = session.scalar(
        select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
    )
    if script is None:
        raise not_found("script")
    return script


def selected_storyboard(session: Session, project_id: UUID) -> StoryboardRun:
    run = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )
    if run is None:
        raise not_found("storyboard")
    return run


def resolve_shot(session: Session, project_id: UUID, shot_id: UUID) -> StoryboardShotRecord:
    """Return a shot only when it belongs to this project's selected storyboard."""
    run = selected_storyboard(session, project_id)
    shot = session.get(StoryboardShotRecord, shot_id)
    if shot is None or shot.storyboard_run_id != run.id:
        raise not_found("shot")
    return shot


def current_render(session: Session, project_id: UUID) -> RenderJob | None:
    selected = session.scalar(
        select(RenderJob).where(RenderJob.project_id == project_id, RenderJob.selected.is_(True))
    )
    if selected is not None:
        return selected
    return session.scalar(
        select(RenderJob)
        .where(RenderJob.project_id == project_id)
        .order_by(RenderJob.created_at.desc(), RenderJob.id.desc())
    )


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------


def selected_video_shot_ids(session: Session, run: StoryboardRun) -> list[UUID]:
    """Shot IDs of this storyboard run that currently have a selected video."""
    return list(
        session.scalars(
            select(AnimationGeneratedVideo.shot_id)
            .join(
                StoryboardShotRecord,
                StoryboardShotRecord.id == AnimationGeneratedVideo.shot_id,
            )
            .where(
                StoryboardShotRecord.storyboard_run_id == run.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).all()
    )


def selected_video_asset_ids(session: Session, run: StoryboardRun) -> list[UUID]:
    """Canonical asset IDs of this storyboard run's selected videos."""
    return list(
        session.scalars(
            select(AnimationGeneratedVideo.canonical_asset_id)
            .join(
                StoryboardShotRecord,
                StoryboardShotRecord.id == AnimationGeneratedVideo.shot_id,
            )
            .where(
                StoryboardShotRecord.storyboard_run_id == run.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        ).all()
    )


def project_summary(
    session: Session, project: Project, versions: RowVersionService
) -> ProjectSummaryProjection:
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project.id))
    committed = sum(
        (
            row.actual_amount
            for row in session.scalars(
                select(CostLedgerEntry).where(CostLedgerEntry.project_id == project.id)
            )
        ),
        Decimal(0),
    )
    latest_failure = session.scalar(
        select(PipelineFailureEvent)
        .where(PipelineFailureEvent.project_id == project.id)
        .order_by(PipelineFailureEvent.created_at.desc())
        .limit(1)
    )
    has_failures = latest_failure is not None
    run = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == project.id)
    )
    if run is not None and run.status == "cancelled":
        effective_status = "cancelled"
    else:
        effective_status = project.status
    stage = WORKFLOW_STAGE_ALIASES.get(effective_status)
    return ProjectSummaryProjection(
        project_id=project.id,
        name=project.name,
        status=effective_status,
        run_state=project_run_state(run, project_status=project.status),
        current_stage=stage,
        progress_percentage=None,
        target_duration_seconds=project.target_duration_seconds,
        visual_style=project.visual_style,
        humor_intensity=project.humor_intensity,
        updated_at=utc(project.updated_at) or datetime.now(UTC),
        committed_cost_amount=str(committed),
        hard_cap_amount=str(budget.hard_cap) if budget else None,
        has_failures=has_failures,
        latest_failure_stage=latest_failure.stage if latest_failure else None,
        latest_failure_code=latest_failure.error_code if latest_failure else None,
        row_version=versions.current(project.id, "project", project.id),
    )


def _stage_timeline(
    completed: list[str],
    current: PipelineStage | None,
    cancelled: bool,
    failed: PipelineStage | None = None,
) -> list[StageTimelineEntry]:
    completed_stages = {
        WORKFLOW_STAGE_ALIASES[name] for name in completed if name in WORKFLOW_STAGE_ALIASES
    }
    entries: list[StageTimelineEntry] = []
    for stage in PIPELINE_STAGE_ORDER:
        if stage in completed_stages:
            state = StageState.COMPLETE
        elif failed is not None and stage == failed:
            # A workflow that died cannot be queried, so the only witness to
            # *where* it died is the failure it recorded on its way out. Without
            # this the timeline draws every stage as pending and the dashboard
            # offers no stage to retry.
            state = StageState.FAILED
        elif cancelled:
            state = StageState.CANCELLED
        elif current is not None and stage == current:
            state = StageState.RUNNING
        else:
            state = StageState.PENDING
        entries.append(StageTimelineEntry(stage=stage, state=state))
    return entries


#: Failure statuses describing something the pipeline got past on its own. A
#: retried provider call leaves an unresolved row behind, and naming its stage
#: as the one that stopped the project would point a retry at the wrong work.
_RECOVERED_FAILURE_STATUSES: frozenset[str] = frozenset({"recovered", "resolved"})


def latest_unresolved_failure(
    failures: Sequence[PipelineFailureEvent],
) -> PipelineFailureEvent | None:
    """The most recent failure nobody has marked resolved, if there is one."""
    unresolved = [
        failure
        for failure in failures
        if failure.resolved_at is None
        and failure.projected_status not in _RECOVERED_FAILURE_STATUSES
    ]
    if not unresolved:
        return None
    return max(unresolved, key=lambda failure: (failure.created_at, failure.id.hex))


def workflow_status(
    session: Session,
    project: Project,
    run: ProjectWorkflowRun | None,
    workflow_state: Any | None,
) -> WorkflowStatusProjection:
    """Project the parent workflow's compact status onto the UI timeline."""
    completed: list[str] = list(getattr(workflow_state, "completed_stages", []) or [])
    status = str(getattr(workflow_state, "status", None) or (run.status if run else "not_started"))
    cancelled = bool(getattr(workflow_state, "cancelled", False)) or (
        run is not None and run.status == "cancelled"
    )
    current = WORKFLOW_STAGE_ALIASES.get(status)
    storyboard = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project.id, StoryboardRun.selected.is_(True)
        )
    )
    total_shots = storyboard.shot_count if storyboard else 0
    locked = len(selected_video_shot_ids(session, storyboard)) if storyboard else 0
    failures = session.scalars(
        select(PipelineFailureEvent).where(PipelineFailureEvent.project_id == project.id)
    ).all()
    # A run the reconciler settled as failed has no workflow left to query, so
    # the stage it stopped at comes from its own failure record instead.
    latest_failure = latest_unresolved_failure(failures)
    failed_stage = (
        WORKFLOW_STAGE_ALIASES.get(latest_failure.stage)
        if latest_failure is not None and not cancelled and status in _FAILED_WORKFLOW_STATUSES
        else None
    )
    render = current_render(session, project.id)
    started = utc(run.created_at) if run else None
    updated = utc(run.updated_at) if run else None
    elapsed = (
        ((updated or datetime.now(UTC)) - started).total_seconds() if started is not None else None
    )
    # Only report a percentage the shot counts can actually justify.
    percentage = min(locked / total_shots * 100, 100.0) if total_shots else None
    return WorkflowStatusProjection(
        project_id=project.id,
        workflow_id=run.workflow_id if run else None,
        run_id=run.run_id if run else None,
        status=status,
        current_stage=current,
        completed_stages=[
            WORKFLOW_STAGE_ALIASES[name] for name in completed if name in WORKFLOW_STAGE_ALIASES
        ],
        cancelled=cancelled,
        started_at=started,
        updated_at=updated,
        elapsed_seconds=max(elapsed, 0) if elapsed is not None else None,
        total_shot_count=total_shots,
        completed_shot_count=locked,
        failed_shot_count=sum(1 for failure in failures if not failure.retryable),
        retryable_failure_count=sum(1 for failure in failures if failure.retryable),
        render_status=render.status if render else None,
        stages=_stage_timeline(completed, current, cancelled, failed_stage),
        progress_percentage=percentage,
    )


def transcript_projection(
    session: Session, project_id: UUID, transcript: Transcript, versions: RowVersionService
) -> TranscriptProjection:
    rows = session.scalars(
        select(TranscriptSegmentRecord)
        .where(TranscriptSegmentRecord.transcript_id == transcript.id)
        .order_by(TranscriptSegmentRecord.sequence)
    ).all()
    return TranscriptProjection(
        transcript_id=transcript.id,
        project_id=project_id,
        version=transcript.version,
        language=transcript.language,
        origin="transcription" if transcript.run_id is not None else "subtitle",
        duration_seconds=transcript.duration_seconds,
        coverage_score=transcript.coverage_score,
        selected=transcript.selected,
        row_version=versions.current(project_id, "transcript", transcript.id),
        source_asset_id=transcript.transcript_asset_id,
        segments=[
            TranscriptSegmentProjection(
                segment_id=row.id,
                sequence=row.sequence,
                start_seconds=row.start_seconds,
                end_seconds=row.end_seconds,
                text=row.text,
                speaker_label=row.speaker_label,
                confidence=row.confidence,
                edited=bool((row.provenance or {}).get("edited_by_owner")),
                row_version=versions.current(project_id, "transcript_segment", row.id),
            )
            for row in rows
        ],
    )


def script_projection(
    session: Session, project_id: UUID, script: Script, versions: RowVersionService
) -> ScriptProjection:
    rows = session.scalars(
        select(ScriptSegment)
        .where(ScriptSegment.script_id == script.id)
        .order_by(ScriptSegment.sequence)
    ).all()
    measured = {
        segment.script_segment_id: segment
        for segment in session.scalars(
            select(NarrationSegment).where(
                NarrationSegment.script_segment_id.in_([row.id for row in rows] or [None])
            )
        ).all()
    }
    return ScriptProjection(
        project_id=project_id,
        script=script_summary(project_id, script, versions),
        approved=script.status in {"approved", "selected"},
        segments=[
            ScriptSegmentProjection(
                segment_id=row.id,
                stable_segment_id=row.stable_segment_id,
                sequence=row.sequence,
                segment_type=row.segment_type,
                speaker_kind=row.speaker_kind,
                speaker_label=row.anonymous_speaker_label,
                text=row.text,
                visual_gag=row.visual_gag,
                joke_annotation_count=len(row.joke_annotations or []),
                plot_beat_ids=[str(item) for item in (row.plot_beat_ids or [])],
                word_count=len(row.text.split()),
                estimated_duration_ms=row.estimated_duration_ms,
                measured_narration_duration_ms=_measured_ms(measured.get(row.id)),
                locked=row.locked,
                content_hash=row.content_hash,
                row_version=versions.current(project_id, "script_segment", row.id),
            )
            for row in rows
        ],
    )


def _measured_ms(segment: NarrationSegment | None) -> int | None:
    if segment is None:
        return None
    duration = getattr(segment, "duration_seconds", None)
    return round(duration * 1000) if isinstance(duration, int | float) else None


def script_summary(
    project_id: UUID, script: Script, versions: RowVersionService
) -> ScriptSummaryProjection:
    return ScriptSummaryProjection(
        script_id=script.id,
        version=script.version,
        status=script.status,
        selected=script.selected,
        actual_word_count=script.actual_word_count,
        target_word_count=script.target_word_count,
        target_duration_ms=script.target_duration_ms,
        parent_script_id=script.parent_script_id,
        editing_pass=script.editing_pass,
        rejection_reason=script.rejection_reason,
        created_at=utc(script.created_at) or datetime.now(UTC),
        row_version=versions.current(project_id, "script", script.id),
    )


def _shot_costs(session: Session, shot_id: UUID) -> Decimal:
    """Sum the T23 ledger through the provider attempts recorded for this shot."""
    rows = session.scalars(
        select(CostLedgerEntry)
        .join(ProviderAttempt, ProviderAttempt.id == CostLedgerEntry.provider_attempt_id)
        .where(
            ProviderAttempt.related_entity_type == "storyboard_shot",
            ProviderAttempt.related_entity_id == shot_id,
        )
    ).all()
    return sum((row.actual_amount for row in rows), Decimal(0))


#: Control-command statuses that leave nothing for a reviewer to see: the
#: command either did its work (and the shot's own rows now describe it) or was
#: withdrawn before it started. A *failed* command is deliberately not here -
#: the reviewer has to be told why, and have the action offered back.
_RESOLVED_COMMAND_STATUSES = frozenset({"completed", "cancelled", "superseded"})

#: Statuses a command can still leave on its own. ``awaiting_review`` is one of
#: them, and is the odd one out: the command is durably parked *on a person*,
#: so it is in flight without the machine doing anything. The UI must not read
#: it as busy - the decision it is waiting for is exactly the one it would
#: otherwise disable, which would deadlock the shot.
_ACTIVE_COMMAND_STATUSES = frozenset(
    {"pending", "claimed", "dispatching", "running", "awaiting_review"}
)

#: Statuses that can never be surfaced, so the projection never loads them. A
#: shot accumulates one completed command per regeneration over a project's
#: life; only the newest of those can matter, which the ordering below picks.
_UNSURFACEABLE_STATUSES = frozenset({"cancelled", "superseded"})

#: Where a command that acts on a shot without targeting one names its shot.
#: A T22 remediation's durable row stays targeted at the final-QA run and
#: carries the shot in metadata; the shot-targeted proxy the dispatcher builds
#: is never persisted, so the row itself is all this projection can read.
_SHOT_METADATA_KEY = "shot_id"


def _shot_command_projection(record: ControlCommandRecord) -> ShotCommandProjection:
    return ShotCommandProjection(
        command_id=record.id,
        command_type=record.command_type,
        status=record.status,
        active=record.status in _ACTIVE_COMMAND_STATUSES,
        awaiting_review=record.status == ControlCommandStatus.AWAITING_REVIEW.value,
        dispatched=record.workflow_id is not None,
        workflow_id=record.workflow_id,
        failure_code=record.error_code,
        failure_summary=record.error_summary,
        retryable=bool(record.retryable),
        cancel_requested=record.cancel_requested_at is not None,
        created_at=utc(record.created_at) or datetime.now(UTC),
        updated_at=utc(record.updated_at) or utc(record.created_at) or datetime.now(UTC),
    )


def _commanded_shot_id(record: ControlCommandRecord, shot_ids: frozenset[UUID]) -> UUID | None:
    """Which of these shots this command acts on, if any."""
    if record.target_type == "shot":
        return record.target_id if record.target_id in shot_ids else None
    raw = dict(record.command_metadata or {}).get(_SHOT_METADATA_KEY)
    if raw is None:
        return None
    try:
        shot_id = UUID(str(raw))
    except ValueError:
        return None
    return shot_id if shot_id in shot_ids else None


def shot_commands(
    session: Session, project_id: UUID, shot_ids: Sequence[UUID]
) -> dict[UUID, ShotCommandProjection]:
    """The unresolved control command acting on each of these shots.

    The T18b command row is the only place the window between "the reviewer
    decided" and "the replacement workflow wrote something" is recorded. The
    shot's own tables still describe the previous attempt for that whole
    window, so a projection built from them alone reads an accepted retry as an
    untouched failure and offers the same button again.

    A shot in flight is the answer whenever there is one, even if a *newer*
    command against the same shot has already finished: taking the newest row
    and then discarding it when resolved would let a quick completion mask work
    that is still running, which is precisely the duplicate this exists to
    prevent. Otherwise only a terminal *failure* is surfaced, and only while it
    is the newest thing that happened to the shot - a failure a later command
    has already succeeded past is history, not something to show.
    """
    if not shot_ids:
        return {}
    wanted = frozenset(shot_ids)
    rows = session.scalars(
        select(ControlCommandRecord)
        .where(
            ControlCommandRecord.project_id == project_id,
            ControlCommandRecord.status.notin_(sorted(_UNSURFACEABLE_STATUSES)),
            or_(
                and_(
                    ControlCommandRecord.target_type == "shot",
                    ControlCommandRecord.target_id.in_(list(wanted)),
                ),
                ControlCommandRecord.command_type
                == ControlCommandType.FINAL_QA_REMEDIATION.value,
            ),
        )
        .order_by(ControlCommandRecord.created_at.desc(), ControlCommandRecord.id.desc())
    ).all()
    # Descending, so the first row seen for a shot is its newest.
    newest: dict[UUID, ControlCommandRecord] = {}
    in_flight: dict[UUID, ControlCommandRecord] = {}
    for row in rows:
        shot_id = _commanded_shot_id(row, wanted)
        if shot_id is None:
            continue
        newest.setdefault(shot_id, row)
        if row.status in _ACTIVE_COMMAND_STATUSES:
            in_flight.setdefault(shot_id, row)
    commands: dict[UUID, ShotCommandProjection] = {}
    for shot_id, row in newest.items():
        chosen = in_flight.get(shot_id)
        if chosen is None and row.status not in _RESOLVED_COMMAND_STATUSES:
            chosen = row
        if chosen is not None:
            commands[shot_id] = _shot_command_projection(chosen)
    return commands


def shot_projection(
    session: Session,
    project_id: UUID,
    shot: StoryboardShotRecord,
    versions: RowVersionService,
    *,
    pending_command: ShotCommandProjection | None = None,
    commands_prefetched: bool = False,
) -> StoryboardShotProjection:
    """One shot, as the grid and the inspector render it.

    ``commands_prefetched`` lets a caller that already batched the shots'
    control commands pass its answer straight through, including the absence of
    one, rather than paying a query per shot.
    """
    if not commands_prefetched:
        pending_command = shot_commands(session, project_id, [shot.id]).get(shot.id)
    contract = shot.contract or {}
    camera = shot.camera or {}
    references = shot.references or {}
    video = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == shot.id,
            AnimationGeneratedVideo.selected.is_(True),
        )
    )
    keyframe = session.scalar(
        select(GeneratedKeyframeImage).where(
            GeneratedKeyframeImage.shot_id == shot.id,
            GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
            GeneratedKeyframeImage.selected.is_(True),
        )
    )
    item = session.scalar(select(AnimationItem).where(AnimationItem.shot_id == shot.id))
    status = item.status if item is not None else ("locked" if video is not None else "pending")
    return StoryboardShotProjection(
        shot_id=shot.id,
        stable_shot_id=shot.stable_shot_id,
        global_sequence=shot.global_sequence,
        segment_sequence=shot.segment_sequence,
        script_segment_id=shot.script_segment_id,
        global_start_us=shot.global_start_us,
        global_end_us=shot.global_end_us,
        usable_duration_us=shot.usable_duration_us,
        requested_generation_duration_us=shot.requested_generation_duration_us,
        trim_start_us=shot.trim_start_us,
        trim_end_us=shot.trim_end_us,
        visual_objective=str(contract.get("visual_objective", ""))[:2000],
        camera_framing=_short(camera.get("framing")),
        camera_movement=_short(camera.get("movement")),
        character_references=[str(item) for item in references.get("character_reference_ids", [])][
            :32
        ],
        location_reference=_short(references.get("location_reference_id"), 255),
        transition_in=_short((shot.transition_in or {}).get("kind")),
        transition_out=_short((shot.transition_out or {}).get("kind")),
        workflow_status=status,
        selected_keyframe_asset_id=keyframe.asset_id if keyframe else None,
        selected_video_asset_id=video.canonical_asset_id if video else None,
        provider=item.provider if item else None,
        model=item.model if item else None,
        attempt_count=item.attempt_count if item else 0,
        cost_amount=str(_shot_costs(session, shot.id)),
        warning_code=_first_code(item.warnings if item else None),
        failure_code=item.error_code if item else None,
        pending_command=pending_command,
        row_version=versions.current(project_id, "shot", shot.id),
    )


def storyboard_projection(
    session: Session, project_id: UUID, run: StoryboardRun, versions: RowVersionService
) -> StoryboardProjection:
    shots = session.scalars(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == run.id)
        .order_by(StoryboardShotRecord.global_sequence)
    ).all()
    commands = shot_commands(session, project_id, [shot.id for shot in shots])
    return StoryboardProjection(
        project_id=project_id,
        storyboard_run_id=run.id,
        version=run.version,
        selected=run.selected,
        shot_count=run.shot_count,
        segment_count=run.segment_count,
        total_duration_us=run.total_duration_us,
        timing_manifest_asset_id=run.timing_manifest_asset_id,
        row_version=versions.current(project_id, "storyboard", run.id),
        shots=[
            shot_projection(
                session,
                project_id,
                shot,
                versions,
                pending_command=commands.get(shot.id),
                commands_prefetched=True,
            )
            for shot in shots
        ],
    )


def shot_detail(
    session: Session, project_id: UUID, shot: StoryboardShotRecord, versions: RowVersionService
) -> ShotDetailProjection:
    projection = shot_projection(session, project_id, shot, versions)
    keyframes = session.scalars(
        select(GeneratedKeyframeImage)
        .where(GeneratedKeyframeImage.shot_id == shot.id)
        .order_by(GeneratedKeyframeImage.keyframe_role)
    ).all()
    image_item = session.scalar(
        select(ImageGenerationItem).where(ImageGenerationItem.shot_id == shot.id)
    )
    videos = session.scalars(
        select(AnimationGeneratedVideo)
        .where(AnimationGeneratedVideo.shot_id == shot.id)
        .order_by(AnimationGeneratedVideo.created_at)
    ).all()
    item = session.scalar(select(AnimationItem).where(AnimationItem.shot_id == shot.id))
    tasks = (
        {
            task.provider_attempt_id: task
            for task in session.scalars(
                select(RunwayTask).where(RunwayTask.animation_item_id == item.id)
            ).all()
        }
        if item is not None
        else {}
    )
    # One entry per recorded regeneration of this shot, timestamped, rather than
    # the shot's own ID repeated once per invalidated downstream artifact.
    regenerations = [
        (utc(row.created_at) or row.created_at).isoformat()
        for row in session.scalars(
            select(DownstreamInvalidation)
            .where(
                DownstreamInvalidation.project_id == project_id,
                DownstreamInvalidation.origin_type == "shot",
                DownstreamInvalidation.origin_id == shot.id,
                DownstreamInvalidation.invalidated_type == "shot",
            )
            .order_by(DownstreamInvalidation.created_at)
        ).all()
    ]
    return ShotDetailProjection(
        shot=projection,
        child_workflow_id=(item.generation_identity[:24] if item is not None else None),
        child_workflow_status=projection.workflow_status,
        child_workflow_retryable=bool(item and item.status == "failed" and item.error_code),
        identity_hash=item.generation_identity if item is not None else None,
        trim_instructions_asset_id=None,
        source_evidence_ids=[
            str(reference.get("source_asset_id"))
            for reference in (shot.contract or {}).get("evidence_references", [])
            if isinstance(reference, dict) and reference.get("source_asset_id")
        ][:64],
        keyframe_attempts=[
            ShotAttemptProjection(
                attempt_id=row.id,
                kind="keyframe",
                attempt_number=index + 1,
                status="succeeded",
                asset_id=row.asset_id,
                provider=row.provider,
                model=row.model,
                provider_task_id=None,
                generation_identity=(
                    image_item.generation_identity if image_item is not None else None
                ),
                prompt_version=None,
                cost_amount=None,
                failure_class=None,
                selected=row.selected,
                created_at=datetime.now(UTC),
            )
            for index, row in enumerate(keyframes)
        ],
        video_attempts=[
            ShotAttemptProjection(
                attempt_id=row.id,
                kind="video",
                attempt_number=index + 1,
                status="succeeded",
                asset_id=row.canonical_asset_id,
                provider=item.provider if item else "unknown",
                model=item.model if item else "unknown",
                provider_task_id=_task_id(tasks.get(row.provider_attempt_id)) or row.remote_task_id,
                generation_identity=item.generation_identity if item else None,
                prompt_version=None,
                generated_duration_us=round(row.provider_duration * 1_000_000),
                usable_duration_us=round(row.canonical_duration * 1_000_000),
                cost_amount=None,
                failure_class=None,
                selected=row.selected,
                created_at=utc(row.created_at) or datetime.now(UTC),
            )
            for index, row in enumerate(videos)
        ],
        regeneration_history=regenerations[:64],
    )


def shot_status(
    session: Session, project_id: UUID, shot: StoryboardShotRecord, versions: RowVersionService
) -> ShotStatusProjection:
    item = session.scalar(select(AnimationItem).where(AnimationItem.shot_id == shot.id))
    video = session.scalar(
        select(AnimationGeneratedVideo).where(
            AnimationGeneratedVideo.shot_id == shot.id,
            AnimationGeneratedVideo.selected.is_(True),
        )
    )
    return ShotStatusProjection(
        shot_id=shot.id,
        child_workflow_id=item.generation_identity[:24] if item is not None else None,
        status=item.status if item is not None else ("locked" if video else "pending"),
        retryable=bool(item and item.status == "failed" and item.error_code),
        attempt_count=item.attempt_count if item is not None else 0,
        failure_code=item.error_code if item is not None else None,
        pending_command=shot_commands(session, project_id, [shot.id]).get(shot.id),
        row_version=versions.current(project_id, "shot", shot.id),
    )


def render_projection(
    session: Session, project_id: UUID, render: RenderJob, versions: RowVersionService
) -> RenderProjection:
    captions = session.scalar(
        select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == render.id)
    )
    verified = (
        render.status == "render_complete" and render.verification_report_asset_id is not None
    )
    # The render's own storyboard run, never whatever is selected now: a later
    # run's shots must not silently invalidate this render's approval.
    render_run = (
        session.get(StoryboardRun, render.storyboard_run_id)
        if render.storyboard_run_id is not None
        else None
    )
    selected_videos = selected_video_asset_ids(session, render_run) if render_run else []
    lineage = render_lineage_hash(
        project_id=project_id,
        script_id=render.script_id,
        script_version=render.script_version,
        narration_run_id=render.narration_run_id,
        storyboard_run_id=render.storyboard_run_id,
        render_identity=render.render_identity,
        caption_identity=captions.caption_identity if captions else None,
        selected_video_asset_ids=list(selected_videos),
    )
    stale = render_is_stale(session, project_id, render)
    approval = session.scalar(
        select(RenderApproval)
        .where(RenderApproval.render_job_id == render.id, RenderApproval.revoked_at.is_(None))
        .order_by(RenderApproval.approved_at.desc())
    )
    audio = render.audio_profile or {}
    return RenderProjection(
        render_job_id=render.id,
        project_id=project_id,
        status=render.status,
        attempt=max(render.attempt, 1),
        render_version=render.pipeline_version,
        render_identity=render.render_identity,
        selected=render.selected,
        stale=stale,
        verified=verified,
        verification_summary=("verification report attached" if verified else None),
        expected_duration_us=render.expected_duration_us,
        measured_duration_us=render.measured_duration_us,
        selected_shot_count=len(selected_videos),
        caption_language=captions.language if captions else None,
        caption_cue_count=captions.cue_count if captions else None,
        subtitle_mode=str((render.caption_profile or {}).get("mode", "external")),
        integrated_loudness_lufs=_number(audio.get("integrated_loudness_lufs")),
        true_peak_dbtp=_number(audio.get("true_peak_dbtp")),
        warning_codes=[str(code)[:64] for code in (render.error or {}).get("warnings", [])][:32],
        final_video_asset_id=render.final_video_asset_id,
        srt_asset_id=render.srt_asset_id,
        webvtt_asset_id=render.webvtt_asset_id,
        verification_report_asset_id=render.verification_report_asset_id,
        manifest_asset_id=render.manifest_asset_id,
        script_id=render.script_id,
        script_version=render.script_version,
        storyboard_run_id=render.storyboard_run_id,
        narration_run_id=render.narration_run_id,
        ffmpeg_version=render.ffmpeg_version,
        lineage_hash=lineage,
        progress_percent=max(0, min(100, render.progress_percent)),
        checkpoint=render.checkpoint,
        attempt_count=max(render.attempt_count, 0),
        cancel_requested=render.cancel_requested,
        failure_code=render.error_code,
        failure_classification=render.failure_classification,
        output_sha256=render.output_sha256,
        input_hash=render.input_hash,
        renderer_version=render.renderer_version,
        downloadable=verified and not stale and render.final_video_asset_id is not None,
        approval=(
            RenderApprovalProjection(
                approval_id=approval.id,
                render_job_id=approval.render_job_id,
                approved_by=approval.approved_by,
                approved_at=utc(approval.approved_at) or datetime.now(UTC),
                lineage_hash=approval.lineage_hash,
                applies_to_current_lineage=approval.lineage_hash == lineage,
            )
            if approval is not None
            else None
        ),
        row_version=versions.current(project_id, "render", render.id),
        completed_at=utc(render.completed_at),
    )


def render_is_stale(session: Session, project_id: UUID, render: RenderJob) -> bool:
    """A render is stale once anything it depends on was invalidated after it ran."""
    return (
        session.scalar(
            select(DownstreamInvalidation.id)
            .where(
                DownstreamInvalidation.project_id == project_id,
                DownstreamInvalidation.invalidated_type == "render",
                DownstreamInvalidation.invalidated_id == render.id,
            )
            .limit(1)
        )
        is not None
    )


def _short(value: object, limit: int = 64) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _first_code(warnings: list[dict[str, Any]] | None) -> str | None:
    for warning in warnings or []:
        code = warning.get("code") if isinstance(warning, dict) else None
        if isinstance(code, str):
            return code[:64]
    return None


def _task_id(task: RunwayTask | None) -> str | None:
    return task.remote_task_id if task is not None else None
