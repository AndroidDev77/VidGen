"""What each durable control command actually does when it is dispatched.

Every handler here ends in exactly one of three ways:

* it started or adopted a real Temporal workflow and returns that workflow's
  actual identity, which is what lets the command become ``running``;
* it produced a durable resource with no workflow to wait on, and returns a
  result the command completes with immediately;
* it raised :class:`DispatchFailure`, which the dispatcher records with an
  actionable code and either retries within the command's bound or fails.

No handler invents a workflow ID, and no handler performs provider work itself:
paid work happens inside the workflow the handler started, under T23's existing
provider-attempt and budget accounting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.control_plane.generation_runs import (
    GenerationRunService,
    generation_input_identity,
)
from services.control_plane.lineage import (
    LineageUnavailable,
    selected_render,
    upstream_identity,
)
from services.control_plane.references import (
    ReferenceInputsUnavailable,
    resolve_reference_inputs,
)
from services.control_plane.shot_commands import SEQUENCE_KEY, next_regeneration_sequence
from services.render_execution.commands import queue_render_job
from services.renderer.selection import RenderLineageError
from services.review.shot_identity import (
    configuration_identities,
    current_workflow_id,
    shot_workflow_identity,
)
from vidgen.contracts.continuity_workflow import (
    ReferenceApprovalSignal,
    ReferenceWorkflowInput,
)
from vidgen.contracts.control_commands import (
    ControlCommandResult,
    ControlCommandTargetType,
    ControlCommandType,
    ProjectGenerationRunStatus,
)
from vidgen.contracts.final_editorial import FinalRemediationTarget
from vidgen.contracts.shot_workflow import ShotWorkflowInput, ShotWorkflowStatus
from vidgen.contracts.workflow import (
    FinalQAActivityInput,
    ProjectWorkflowInput,
    RenderActivityInput,
)
from vidgen.db.continuity_models import character_reference_sets, location_reference_sets
from vidgen.db.control_command_models import ControlCommandRecord
from vidgen.db.models import Project, SourceVideo
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.review.workflow_control import WorkflowController, reference_workflow_id

#: Namespace for deterministic approval IDs, so a replayed apply command carries
#: the same approval identity and the workflow deduplicates it.
APPROVAL_NAMESPACE = UUID("6b2c1f13-2b6f-5a2b-9d0a-8b7c4e2a1f90")


class DispatchFailure(RuntimeError):
    """A structured dispatch failure with an owner-renderable code."""

    def __init__(self, code: str, summary: str, *, retryable: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DispatchContext:
    """Everything a handler is allowed to reach. Deliberately small."""

    session: Session
    controller: WorkflowController
    image_provider_name: str
    image_model: str
    video_provider_name: str
    visual_capability_profile: str


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What dispatch achieved: a started workflow, or a finished result."""

    workflow_id: str | None = None
    run_id: str | None = None
    result: ControlCommandResult | None = None
    awaiting_reason: str = ""
    #: Set when the command produced its result without any workflow to wait on.
    completed: bool = False


Handler = Callable[[DispatchContext, ControlCommandRecord], DispatchOutcome]


def _project(context: DispatchContext, record: ControlCommandRecord) -> Project:
    project = context.session.get(Project, record.project_id)
    if project is None or project.owner_subject != record.owner_subject:
        # Ownership is revalidated at dispatch, not only at submission: a
        # command must not outlive the authorization that created it.
        raise DispatchFailure(
            "command_owner_revoked",
            "The project no longer exists or is no longer owned by this actor.",
        )
    return project


def _source_video_id(context: DispatchContext, project_id: UUID) -> UUID:
    source = context.session.scalar(
        select(SourceVideo)
        .where(SourceVideo.project_id == project_id)
        .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
    )
    if source is None:
        raise DispatchFailure("source_video_missing", "This project has no completed source video.")
    return source.id


# -- T19 ------------------------------------------------------------------
def _reference_inputs(
    context: DispatchContext,
    record: ControlCommandRecord,
    project_id: UUID,
    *,
    entity_id: UUID | None = None,
) -> ReferenceWorkflowInput:
    try:
        return resolve_reference_inputs(
            context.session,
            project_id=project_id,
            idempotency_key=f"command:{record.id}",
            entity_id=entity_id,
        )
    except ReferenceInputsUnavailable as error:
        raise DispatchFailure(error.code, error.summary) from error


def dispatch_reference_build(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Start (or adopt) the T19 workflow that drafts and awaits approval."""
    project = _project(context, record)
    # A per-entity regeneration is the same workflow with a narrower scope. The
    # entity is part of the reference run's own identity, so it gets its own
    # workflow and drafts only that character or location - it can neither
    # adopt nor be adopted by the project-wide build.
    entity_id = (
        record.target_id
        if record.command_type == ControlCommandType.REFERENCE_GENERATE.value
        else None
    )
    request = _reference_inputs(context, record, project.id, entity_id=entity_id)
    workflow_id, run_id = context.controller.start_references(request)
    return DispatchOutcome(
        workflow_id=workflow_id,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.REFERENCE_SET,
            result_id=request.reference_run_id,
            summary={"reference_run_id": str(request.reference_run_id)},
        ),
        awaiting_reason="reference_approval_required",
    )


def dispatch_reference_apply(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Deliver the owner's approvals to the waiting T19 workflow.

    The signal names the exact approved reference sets. If no workflow is
    waiting - the project was never started, or its run has closed - one is
    started for the same reference run and then signalled, so an approval is
    never a row update with nothing behind it.
    """
    project = _project(context, record)
    request = _reference_inputs(context, record, project.id)
    approved = _approved_reference_set_ids(context.session, project.id)
    signal = ReferenceApprovalSignal(
        project_id=project.id,
        reference_run_id=request.reference_run_id,
        approval_id=uuid5(APPROVAL_NAMESPACE, f"{record.id}"),
        idempotency_key=f"approval:{record.id}",
        storyboard_run_id=request.storyboard_run_id,
        approved_reference_set_ids=approved,
    )
    workflow_id = reference_workflow_id(request.reference_run_id)
    if not context.controller.signal_reference_approval(workflow_id, signal):
        workflow_id, run_id = context.controller.start_references(request)
        if not context.controller.signal_reference_approval(workflow_id, signal):
            raise DispatchFailure(
                "reference_workflow_unavailable",
                "The continuity workflow could not accept this approval.",
                retryable=True,
            )
    else:
        run_id = None
    return DispatchOutcome(
        workflow_id=workflow_id,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.REFERENCE_SET,
            result_id=request.reference_run_id,
            summary={"approved_reference_sets": str(len(approved))},
        ),
    )


def _approved_reference_set_ids(session: Session, project_id: UUID) -> list[UUID]:
    approved: list[UUID] = []
    for table in (character_reference_sets, location_reference_sets):
        approved.extend(
            UUID(str(value))
            for value in session.scalars(
                select(table.c.id).where(
                    table.c.project_id == project_id, table.c.status == "approved"
                )
            )
        )
    return sorted(approved, key=str)


# -- T16/T18 shots --------------------------------------------------------
def _replacement_shot_input(
    context: DispatchContext, record: ControlCommandRecord, *, sequence: int
) -> ShotWorkflowInput:
    """Build the replacement child's input from reproducible persisted material."""
    shot = context.session.get(StoryboardShotRecord, record.target_id)
    if shot is None:
        raise DispatchFailure("shot_not_found", "That shot no longer exists.")
    run = context.session.get(StoryboardRun, shot.storyboard_run_id)
    if run is None or run.project_id != record.project_id:
        raise DispatchFailure("shot_not_found", "That shot no longer exists.")
    t14_identity, t15_identity = configuration_identities(
        image_provider_name=context.image_provider_name,
        image_model=context.image_model,
        video_provider_name=context.video_provider_name,
        visual_capability_profile=context.visual_capability_profile,
    )
    identity = shot_workflow_identity(
        context.session,
        run,
        shot,
        t14_configuration_identity=t14_identity,
        t15_capability_profile_identity=t15_identity,
        regeneration_sequence=sequence,
    )
    return ShotWorkflowInput(
        project_id=record.project_id,
        storyboard_run_id=run.id,
        storyboard_shot_id=shot.stable_shot_id,
        shot_input_hash=identity.identity_hash,
        workflow_identity=identity,
        idempotency_key=f"t18b:{record.id}:{identity.identity_hash}"[:255],
        trace_context={
            key: str(value)[:128] for key, value in dict(record.trace_context or {}).items()
        },
    )


def _regeneration_sequence(context: DispatchContext, record: ControlCommandRecord) -> int:
    """The sequence this command's replacement child takes.

    Read back from the command row rather than recounted, so a retried dispatch
    resolves to the same identity and adopts the same replacement child instead
    of paying for another one.

    A command created without one - by a caller that could not know at
    submission time whether a replacement would be needed - has the sequence
    minted and *persisted* here, on its first dispatch, so every later attempt
    reads the same value.
    """
    metadata = dict(record.command_metadata or {})
    raw = metadata.get(SEQUENCE_KEY)
    if raw is not None:
        try:
            sequence = int(str(raw))
        except (TypeError, ValueError) as error:
            raise DispatchFailure(
                "regeneration_sequence_invalid",
                "This command's replacement identity is not reproducible.",
            ) from error
        if sequence >= 1:
            return sequence
    sequence = next_regeneration_sequence(
        context.session,
        record.project_id,
        record.target_id,
        exclude_command_id=record.id,
    )
    metadata[SEQUENCE_KEY] = str(sequence)
    record.command_metadata = metadata
    context.session.flush()
    return sequence


def dispatch_shot_regenerate(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Start a genuinely new child workflow for exactly one shot.

    The locked child that currently owns the shot is left alone: it has already
    completed, its attempts and assets remain the project's history, and the
    replacement competes with it only when it passes every gate.
    """
    _project(context, record)
    request = _replacement_shot_input(
        context, record, sequence=_regeneration_sequence(context, record)
    )
    workflow_id, run_id = context.controller.start_shot(request)
    return DispatchOutcome(
        workflow_id=workflow_id,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.SHOT,
            result_id=record.target_id,
            summary={"identity_hash": request.shot_input_hash},
        ),
    )


def dispatch_shot_retry(context: DispatchContext, record: ControlCommandRecord) -> DispatchOutcome:
    """Resume a live shot workflow, or start an immutable recovery run.

    A terminal child is never signalled: a shot whose workflow has closed is
    recovered by starting a new immutable run with the next regeneration
    sequence, which reruns T14, T20, T15, T20 and T21 exactly as policy
    requires and leaves every previous attempt intact.
    """
    _project(context, record)
    shot = context.session.get(StoryboardShotRecord, record.target_id)
    run = context.session.get(StoryboardRun, shot.storyboard_run_id) if shot else None
    if shot is None or run is None:
        raise DispatchFailure("shot_not_found", "That shot no longer exists.")
    t14_identity, t15_identity = configuration_identities(
        image_provider_name=context.image_provider_name,
        image_model=context.image_model,
        video_provider_name=context.video_provider_name,
        visual_capability_profile=context.visual_capability_profile,
    )
    live_id = current_workflow_id(
        shot_workflow_identity(
            context.session,
            run,
            shot,
            t14_configuration_identity=t14_identity,
            t15_capability_profile_identity=t15_identity,
        )
    )
    progress = context.controller.describe_shot_by_id(live_id)
    if progress is not None and progress.state not in {
        ShotWorkflowStatus.LOCKED,
        ShotWorkflowStatus.CANCELLED,
    }:
        # The child is alive and not terminal: resuming it is the cheapest and
        # the only correct answer, because it still owns its durable checkpoint.
        from vidgen.contracts.shot_workflow import ShotWorkflowCommand

        result = context.controller.send_shot_command(
            live_id,
            ShotWorkflowCommand(
                command_id=f"t18b-{record.id}"[:128],
                project_id=record.project_id,
                storyboard_shot_id=shot.stable_shot_id,
                command="retry" if record.command_type == "shot_retry" else "resume",
            ),
        )
        if result.accepted:
            return DispatchOutcome(
                workflow_id=live_id,
                result=ControlCommandResult(
                    result_type=ControlCommandTargetType.SHOT,
                    result_id=record.target_id,
                    summary={"resumed": "true", "code": result.code},
                ),
            )
    return dispatch_shot_regenerate(context, record)


# -- T22 ------------------------------------------------------------------
def dispatch_final_qa_run(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Run T22 against the project's current selected render, out of band."""
    from packages.workflows.control import final_qa_workflow_id

    project = _project(context, record)
    render = selected_render(context.session, project.id)
    if render is None or render.final_video_asset_id is None:
        raise DispatchFailure("render_not_complete", "Final QA needs a selected, completed render.")
    provider = str(dict(record.command_metadata or {}).get("provider", "fake"))
    request = FinalQAActivityInput(
        project_id=project.id,
        final_render_asset_id=render.final_video_asset_id,
        render_manifest_asset_id=render.manifest_asset_id,
        provider="openai" if provider == "openai" else "fake",
        idempotency_key=f"t18b-final-qa:{record.id}"[:255],
    )
    workflow_id = final_qa_workflow_id(project.id, str(record.id))
    started, run_id = context.controller.start_final_qa(request, workflow_id)
    return DispatchOutcome(
        workflow_id=started,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.RENDER_JOB,
            result_id=render.id,
            summary={"render_job_id": str(render.id)},
        ),
    )


#: Remediation targets whose repair is a new render of the same shots.
_RERENDER_TARGETS = frozenset(
    {
        FinalRemediationTarget.RERENDER_T17,
        FinalRemediationTarget.REBUILD_CAPTIONS_T17,
        FinalRemediationTarget.REMIX_AUDIO_T17,
    }
)
#: Remediation targets whose repair is a new run of the shot pipeline.
_SHOT_TARGETS = frozenset(
    {FinalRemediationTarget.REGENERATE_SHOT_T16, FinalRemediationTarget.REPAIR_SHOT_T21}
)
#: Targets that are a routing decision only. They are refused at the API with an
#: explicit unsupported action rather than accepted as executable work.
UNSUPPORTED_REMEDIATION_TARGETS = frozenset(
    {
        FinalRemediationTarget.NONE,
        FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW,
        FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
        FinalRemediationTarget.CORRECT_REFERENCE_T19,
    }
)


def dispatch_final_qa_remediation(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Execute the stage that owns the routed findings' repair.

    The remediation command *is* the owning stage's work: it starts the same
    render or shot workflow that stage would start on its own, so there is never
    a routed finding with nothing behind it.
    """
    target = FinalRemediationTarget(
        str(dict(record.command_metadata or {}).get("target", FinalRemediationTarget.NONE.value))
    )
    if target in _RERENDER_TARGETS:
        return dispatch_render_rerender(context, record)
    if target in _SHOT_TARGETS:
        shot_id = dict(record.command_metadata or {}).get("shot_id")
        if not shot_id:
            raise DispatchFailure(
                "remediation_shot_missing",
                "A shot remediation must name the shot it repairs.",
            )
        proxy = _shot_proxy(record, UUID(str(shot_id)))
        return dispatch_shot_regenerate(context, proxy)
    raise DispatchFailure(
        "remediation_target_unsupported",
        f"{target.value} is a routing classification, not executable work.",
    )


def _shot_proxy(record: ControlCommandRecord, shot_id: UUID) -> ControlCommandRecord:
    """A read-only view of the command retargeted at one shot.

    Never added to the session: the durable row stays the remediation command,
    and this only carries the fields the shot handler reads.
    """
    proxy = ControlCommandRecord(
        id=record.id,
        project_id=record.project_id,
        owner_subject=record.owner_subject,
        command_type=ControlCommandType.SHOT_REGENERATE.value,
        target_type=ControlCommandTargetType.SHOT.value,
        target_id=shot_id,
        idempotency_key=record.idempotency_key,
        request_hash=record.request_hash,
        upstream_input_identity=record.upstream_input_identity,
        command_metadata=dict(record.command_metadata or {}),
        trace_context=dict(record.trace_context or {}),
    )
    return proxy


# -- T17b -----------------------------------------------------------------
def dispatch_render_rerender(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Queue a render through the canonical T17b boundary, then drive it.

    ``queue_render_job`` is the only place a render job is created anywhere in
    the repository, and it reuses a compatible completed job rather than
    producing a second render of identical inputs.
    """
    from packages.workflows.control import render_workflow_id

    project = _project(context, record)
    try:
        queued = queue_render_job(
            context.session,
            project.id,
            idempotency_key=f"t18b:{record.id}"[:255],
        )
    except RenderLineageError as error:
        raise DispatchFailure(error.code, str(error)[:500], retryable=error.retryable) from error
    context.session.flush()
    request = RenderActivityInput(
        project_id=project.id,
        render_job_id=queued.job.id,
        idempotency_key=f"t18b-render:{record.id}"[:255],
    )
    workflow_id = render_workflow_id(project.id, str(record.id))
    started, run_id = context.controller.start_render(request, workflow_id)
    return DispatchOutcome(
        workflow_id=started,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.RENDER_JOB,
            result_id=queued.job.id,
            summary={"reused": str(queued.reused).lower(), "input_hash": queued.input_hash},
        ),
    )


# -- revisions and continuation -------------------------------------------
def dispatch_generation_run(
    context: DispatchContext, record: ControlCommandRecord
) -> DispatchOutcome:
    """Open a new generation run and start the project workflow for it.

    This is the single mechanism behind a transcript revision, a script
    revision and an explicit continuation: all three are "run the project again
    from stage X against this material", and all three preserve the previous
    run as history instead of overwriting it.
    """
    project = _project(context, record)
    entry_stage = str(dict(record.command_metadata or {}).get("entry_stage", "upload"))
    runs = GenerationRunService(context.session)
    # Starting the project workflow adopts a live execution rather than
    # replacing it, which is right for a retried start and wrong for a
    # continuation: the adopted execution would keep its own entry stage and
    # this command would later "complete" off work it never asked for. A
    # project whose current run is still executing is therefore a conflict, not
    # something to silently join.
    current = runs.active(project.id)
    if (
        current is not None
        and current.status == ProjectGenerationRunStatus.ACTIVE.value
        and current.workflow_id is not None
        and current.origin_command_id != record.id
    ):
        raise DispatchFailure(
            "project_generation_run_active",
            "This project is already executing a generation run. Wait for it to "
            "finish or reach a review, or cancel it, then continue.",
        )
    try:
        identity = generation_input_identity(
            project_id=project.id,
            entry_stage=entry_stage,
            material={
                "upstream": record.upstream_input_identity,
                "target": str(record.target_id),
            },
        )
    except ValueError as error:  # pragma: no cover - defensive
        raise DispatchFailure("generation_identity_invalid", str(error)[:500]) from error
    run, created = runs.open(
        project_id=project.id,
        entry_stage=entry_stage,
        input_identity=identity,
        origin_command_id=record.id,
    )
    workflow_id, run_id = context.controller.start_project(
        ProjectWorkflowInput(
            project_id=project.id,
            source_video_id=_source_video_id(context, project.id),
            idempotency_key=f"t18b:{run.id}"[:220],
            generation_run_id=run.id,
            entry_stage=entry_stage,
        )
    )
    runs.bind_workflow(run, workflow_id=workflow_id, run_id=run_id)
    return DispatchOutcome(
        workflow_id=workflow_id,
        run_id=run_id,
        result=ControlCommandResult(
            result_type=ControlCommandTargetType.GENERATION_RUN,
            result_id=run.id,
            summary={
                "entry_stage": entry_stage,
                "generation_run_id": str(run.id),
                "reused": str(not created).lower(),
            },
        ),
    )


HANDLERS: dict[ControlCommandType, Handler] = {
    ControlCommandType.REFERENCE_BUILD: dispatch_reference_build,
    ControlCommandType.REFERENCE_GENERATE: dispatch_reference_build,
    ControlCommandType.REFERENCE_APPLY: dispatch_reference_apply,
    ControlCommandType.SHOT_REGENERATE: dispatch_shot_regenerate,
    ControlCommandType.SHOT_RETRY: dispatch_shot_retry,
    ControlCommandType.SHOT_REVIEW_CONTINUE: dispatch_shot_retry,
    ControlCommandType.TRANSCRIPT_REVISION: dispatch_generation_run,
    ControlCommandType.SCRIPT_REVISION: dispatch_generation_run,
    ControlCommandType.PROJECT_CONTINUE: dispatch_generation_run,
    ControlCommandType.FINAL_QA_RUN: dispatch_final_qa_run,
    ControlCommandType.FINAL_QA_REMEDIATION: dispatch_final_qa_remediation,
    ControlCommandType.RENDER_RERENDER: dispatch_render_rerender,
}


def revalidate_upstream(context: DispatchContext, record: ControlCommandRecord) -> None:
    """Refuse a command whose upstream material moved after it was created.

    A stale approval, a stale regeneration or a stale final-QA run would spend
    money producing something the owner never actually asked for.
    """
    command_type = ControlCommandType(record.command_type)
    entry_stage = str(dict(record.command_metadata or {}).get("entry_stage", "upload"))
    shot_identity_hash = dict(record.command_metadata or {}).get("shot_identity_hash")
    try:
        current = upstream_identity(
            context.session,
            project_id=record.project_id,
            command_type=command_type,
            target_id=record.target_id,
            entry_stage=entry_stage,
            shot_identity_hash=str(shot_identity_hash) if shot_identity_hash else None,
        )
    except LineageUnavailable as error:
        raise DispatchFailure(error.code, error.summary) from error
    if current != record.upstream_input_identity:
        raise DispatchFailure(
            "command_upstream_stale",
            "The inputs this command was created against have changed. Review and resubmit.",
        )


def dispatch(context: DispatchContext, record: ControlCommandRecord) -> DispatchOutcome:
    """Revalidate, then run the one handler that owns this command type."""
    command_type = ControlCommandType(record.command_type)
    handler = HANDLERS.get(command_type)
    if handler is None:  # pragma: no cover - HANDLERS covers the enum, tested
        raise DispatchFailure(
            "command_type_unroutable", f"No dispatcher handles {command_type.value}."
        )
    revalidate_upstream(context, record)
    return handler(context, record)


__all__ = [
    "HANDLERS",
    "UNSUPPORTED_REMEDIATION_TARGETS",
    "DispatchContext",
    "DispatchFailure",
    "DispatchOutcome",
    "dispatch",
    "revalidate_upstream",
]
