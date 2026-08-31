"""Owner-scoped project workflow control.

Starting a project reuses one stable workflow ID per project, refuses to start
before the source upload is complete, and is idempotent: a retried request
adopts the existing run instead of creating a second workflow.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from apps.api.routes._common import (
    ControllerDep,
    IdempotencyKeyDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    events_for,
    idempotency_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.control_commands import ControlCommandResponse
from apps.api.schemas.workflows import (
    ContinueWorkflowRequest,
    StartWorkflowRequest,
    StartWorkflowResponse,
    WorkflowStatusResponse,
)
from services.control_plane.commands import ControlPlaneService
from services.control_plane.generation_runs import (
    GenerationRunService,
    generation_input_identity,
)
from services.costs.project_budget import BudgetDeployment, budget_for, startable
from services.narration.voice_profiles import current_selection
from vidgen.contracts.control_commands import (
    ControlCommandTargetType,
    ControlCommandType,
)
from vidgen.contracts.review import ApiErrorCode, PipelineStage
from vidgen.contracts.workflow import PROJECT_STAGE_ORDER, ProjectWorkflowInput
from vidgen.db.models import SourceVideo
from vidgen.db.upload_models import UploadSession
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.errors import conflict
from vidgen.review.projections import workflow_status
from vidgen.review.workflow_control import project_workflow_id

router = APIRouter(prefix="/projects", tags=["workflows"])

START_OPERATION = "workflow:start"
CANCEL_OPERATION = "workflow:cancel"
CONTINUE_OPERATION = "workflow:continue"


@router.post("/{project_id}/workflow:start", response_model=StartWorkflowResponse)
def start_workflow(
    project_id: UUID,
    request: StartWorkflowRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    settings: SettingsDep,
    response: Response,
    idempotency_key: IdempotencyKeyDep = None,
) -> StartWorkflowResponse:
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(START_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(START_OPERATION, str(project_id), key, payload)
    if replayed is not None:
        return StartWorkflowResponse.model_validate(replayed)

    source = session.scalar(
        select(SourceVideo)
        .where(SourceVideo.project_id == project.id)
        .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
    )
    upload = session.scalar(
        select(UploadSession)
        .where(UploadSession.project_id == project.id)
        .order_by(UploadSession.created_at.desc())
    )
    # ``UploadService.finalize`` records the terminal upload status as
    # "complete"; anything else means the source is still in flight.
    if source is None or (upload is not None and upload.status != "complete"):
        raise conflict(
            ApiErrorCode.UPLOAD_INCOMPLETE,
            "The source video upload must complete before the workflow can start.",
        )
    # T12 narration resolves its voice from the project. Refusing here - before
    # a workflow exists and before a single provider call - is the difference
    # between a clear precondition and a paid run that dies at narration.
    if current_selection(session, project) is None:
        raise conflict(
            ApiErrorCode.VOICE_PROFILE_REQUIRED,
            "Select a narration voice profile before starting this project.",
        )
    workflow_id = project_workflow_id(project.id)
    existing = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.workflow_id == workflow_id)
    )
    if existing is None:
        # A paid deployment reserves against the T23 budget at every provider
        # call. Without a positive hard cap with headroom left, the first paid
        # activity would fail inside the workflow; refusing here costs nothing
        # and says why. A fake-provider deployment spends nothing, so a
        # zero-dollar budget starts.
        #
        # This guards starting, not adopting: a project already running has
        # already committed against its cap, and answering a repeated start
        # with 409 rather than its existing run would break the idempotent
        # adopt path for exactly the projects that are furthest along.
        if not startable(budget_for(session, project.id), BudgetDeployment.from_settings(settings)):
            raise conflict(
                ApiErrorCode.PROJECT_BUDGET_REQUIRED,
                "Set a positive project budget before starting this project. "
                "This deployment uses paid providers, and every generation reserves "
                "against the project's hard cap. Set one with "
                "PUT /api/v1/projects/{id}/budget.",
            )
        runs = GenerationRunService(session)
        sidecar_ids = tuple(request.subtitle_asset_ids)
        material: dict[str, str] = {"source_video_id": str(source.id)}
        if sidecar_ids:
            material["subtitle_asset_ids"] = ",".join(
                sorted(str(i) for i in sidecar_ids)
            )
        generation_run, _ = runs.open(
            project_id=project.id,
            entry_stage="upload",
            input_identity=generation_input_identity(
                project_id=project.id,
                entry_stage="upload",
                material=material,
            ),
        )
        started_workflow_id, run_id = controller.start_project(
            ProjectWorkflowInput(
                project_id=project.id,
                source_video_id=source.id,
                idempotency_key=f"t18:{project.id}",
                provider_configuration_version=request.provider_configuration_version,
                generation_run_id=generation_run.id,
                entry_stage="upload",
                sidecar_asset_ids=sidecar_ids,
            )
        )
        runs.bind_workflow(generation_run, workflow_id=started_workflow_id, run_id=run_id)
        existing = ProjectWorkflowRun(
            project_id=project.id,
            workflow_id=started_workflow_id,
            run_id=run_id,
            status="running",
            idempotency_key=f"t18:{project.id}",
        )
        session.add(existing)
        session.flush()
        events_for(session).append(
            project.id,
            event_type="workflow_started",
            status="running",
            stage=PipelineStage.UPLOAD,
            workflow_id=started_workflow_id,
        )

    body = StartWorkflowResponse(
        workflow_id=existing.workflow_id,
        run_id=existing.run_id,
        status=workflow_status(
            session, project, existing, controller.describe_project(existing.workflow_id)
        ),
    )
    idempotency.record(
        START_OPERATION,
        str(project_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, versions_for(session).current(project.id, "project", project.id))
    return body


@router.post("/{project_id}/workflow:cancel", response_model=WorkflowStatusResponse)
def cancel_workflow(
    project_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    idempotency_key: IdempotencyKeyDep = None,
) -> WorkflowStatusResponse:
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CANCEL_OPERATION, idempotency_key)
    replayed = idempotency.replay(CANCEL_OPERATION, str(project_id), key, {})
    if replayed is not None:
        return WorkflowStatusResponse.model_validate(replayed)
    run = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == project.id)
    )
    if run is None:
        raise conflict(
            ApiErrorCode.WORKFLOW_NOT_STARTED,
            "This project has no workflow to cancel.",
        )
    try:
        controller.cancel_project(run.workflow_id)
    except Exception as exc:
        # Temporal raises RPCError("workflow execution already completed") when
        # the workflow finished between the status check and the cancel signal.
        # Treat it as a successful cancel: the workflow is gone either way.
        if "already completed" not in str(exc):
            raise
    run.status = "cancelled"
    session.flush()
    events_for(session).append(
        project.id,
        event_type="workflow_cancelled",
        status="cancelled",
        workflow_id=run.workflow_id,
    )
    body = workflow_status(session, project, run, controller.describe_project(run.workflow_id))
    idempotency.record(
        CANCEL_OPERATION,
        str(project_id),
        key,
        {},
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


@router.get("/{project_id}/workflow", response_model=WorkflowStatusResponse)
def get_workflow(
    project_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
) -> WorkflowStatusResponse:
    project = owned_project(session, project_id, principal)
    run = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == project.id)
    )
    state = controller.describe_project(run.workflow_id) if run is not None else None
    body = workflow_status(session, project, run, state)
    session.commit()
    return body


@router.post(
    "/{project_id}/workflow:continue",
    response_model=ControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def continue_workflow(
    project_id: UUID,
    request: ContinueWorkflowRequest,
    session: SessionDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyDep = None,
) -> ControlCommandResponse:
    """Continue a project that paused, partially completed, or was revised.

    A project workflow now *completes* at every human pause, so continuing it is
    a new immutable generation run rather than a signal to a closed execution.
    The command this creates is what starts that run; the previous run is kept
    as history and its outputs above the entry stage are reused.
    """
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CONTINUE_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(CONTINUE_OPERATION, str(project_id), key, payload)
    if replayed is not None:
        return ControlCommandResponse.model_validate(replayed)
    if request.entry_stage not in PROJECT_STAGE_ORDER:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED, f"Unknown entry stage: {request.entry_stage}"
        )
    if current_selection(session, project) is None:
        raise conflict(
            ApiErrorCode.VOICE_PROFILE_REQUIRED,
            "Select a narration voice profile before continuing this project.",
        )
    outcome = ControlPlaneService(session, principal.subject).submit(
        project,
        command_type=ControlCommandType.PROJECT_CONTINUE,
        target_type=ControlCommandTargetType.PROJECT,
        target_id=project.id,
        idempotency_key=key,
        payload=payload,
        metadata={"entry_stage": request.entry_stage, "reason": request.reason},
        entry_stage=request.entry_stage,
    )
    body = ControlCommandResponse(command=outcome.command)
    idempotency.record(
        CONTINUE_OPERATION,
        str(project_id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body
