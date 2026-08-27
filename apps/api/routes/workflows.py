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
    events_for,
    idempotency_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.workflows import (
    StartWorkflowRequest,
    StartWorkflowResponse,
    WorkflowStatusResponse,
)
from vidgen.contracts.review import ApiErrorCode, PipelineStage
from vidgen.contracts.workflow import ProjectWorkflowInput
from vidgen.db.models import SourceVideo
from vidgen.db.upload_models import UploadSession
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.errors import conflict
from vidgen.review.projections import workflow_status
from vidgen.review.workflow_control import project_workflow_id

router = APIRouter(prefix="/projects", tags=["workflows"])

START_OPERATION = "workflow:start"
CANCEL_OPERATION = "workflow:cancel"


@router.post("/{project_id}/workflow:start", response_model=StartWorkflowResponse)
def start_workflow(
    project_id: UUID,
    request: StartWorkflowRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
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
    if source is None or (upload is not None and upload.status != "completed"):
        raise conflict(
            ApiErrorCode.UPLOAD_INCOMPLETE,
            "The source video upload must complete before the workflow can start.",
        )

    workflow_id = project_workflow_id(project.id)
    existing = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.workflow_id == workflow_id)
    )
    if existing is None:
        started_workflow_id, run_id = controller.start_project(
            ProjectWorkflowInput(
                project_id=project.id,
                source_video_id=source.id,
                idempotency_key=f"t18:{project.id}",
                provider_configuration_version=request.provider_configuration_version,
            )
        )
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
    controller.cancel_project(run.workflow_id)
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
