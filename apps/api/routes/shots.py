"""Owner-scoped shot inspection, targeted retry, regeneration and selection.

Every command targets exactly one shot: siblings keep their locked identities,
attempts and selected assets, and no sibling workflow is restarted.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from apps.api.routes._common import (
    ControllerDep,
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    idempotency_for,
    mutations_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.control_commands import ControlCommandResponse
from apps.api.schemas.shots import (
    RegenerateShotRequest,
    SelectShotAttemptRequest,
    ShotDetailResponse,
    ShotListResponse,
    ShotRegenerationResponse,
    ShotStatusResponse,
)
from vidgen.review.projections import (
    resolve_shot,
    selected_storyboard,
    shot_detail,
    shot_status,
    storyboard_projection,
)

router = APIRouter(prefix="/projects", tags=["shots"])

REGENERATE_OPERATION = "shot:regenerate"
RETRY_OPERATION = "shot:retry"
CANCEL_OPERATION = "shot:cancel"
SELECT_ATTEMPT_OPERATION = "shot:select-attempt"


@router.get("/{project_id}/shots", response_model=ShotListResponse)
def list_shots(project_id: UUID, session: SessionDep, principal: PrincipalDep) -> ShotListResponse:
    project = owned_project(session, project_id, principal)
    run = selected_storyboard(session, project.id)
    body = ShotListResponse(
        items=storyboard_projection(session, project.id, run, versions_for(session)).shots
    )
    session.commit()
    return body


@router.get("/{project_id}/shots/{shot_id}", response_model=ShotDetailResponse)
def get_shot(
    project_id: UUID,
    shot_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
) -> ShotDetailResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    body = shot_detail(session, project.id, shot, versions_for(session))
    session.commit()
    set_etag(response, body.shot.row_version)
    return body


@router.get("/{project_id}/shots/{shot_id}/status", response_model=ShotStatusResponse)
def get_shot_status(
    project_id: UUID,
    shot_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
) -> ShotStatusResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    body = shot_status(session, project.id, shot, versions_for(session))
    session.commit()
    return body


@router.post("/{project_id}/shots/{shot_id}:regenerate", response_model=ShotRegenerationResponse)
def regenerate_shot(
    project_id: UUID,
    shot_id: UUID,
    request: RegenerateShotRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ShotRegenerationResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(REGENERATE_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(REGENERATE_OPERATION, str(shot_id), key, payload)
    if replayed is not None:
        return ShotRegenerationResponse.model_validate(replayed)
    versions = versions_for(session)
    row_version = versions.require(project.id, "shot", shot.id, if_match, label="shot")
    outcome = mutations_for(session, principal, controller).regenerate_shot(
        project,
        shot,
        idempotency_key=key,
        row_version=row_version,
        confirm_invalidation=request.confirm_invalidation,
    )
    idempotency.record(
        REGENERATE_OPERATION,
        str(shot_id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        outcome.result.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, outcome.result.row_version)
    return outcome.result


@router.post(
    "/{project_id}/shots/{shot_id}:retry",
    response_model=ControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_shot(
    project_id: UUID,
    shot_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ControlCommandResponse:
    """Record the durable command that resumes or replaces this shot.

    The response is the command, not a shot status, because that is the honest
    answer: the shot has not changed yet, and the command is the thing the
    caller can poll, cancel and retry until it has.
    """
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(RETRY_OPERATION, idempotency_key)
    replayed = idempotency.replay(RETRY_OPERATION, str(shot_id), key, {})
    if replayed is not None:
        return ControlCommandResponse.model_validate(replayed)
    versions = versions_for(session)
    versions.require(project.id, "shot", shot.id, if_match, label="shot")
    command = mutations_for(session, principal, controller).retry_shot(
        project, shot, idempotency_key=key
    )
    body = ControlCommandResponse(command=command)
    idempotency.record(
        RETRY_OPERATION,
        str(shot_id),
        key,
        {},
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


@router.post("/{project_id}/shots/{shot_id}:cancel", response_model=ShotStatusResponse)
def cancel_shot(
    project_id: UUID,
    shot_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ShotStatusResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CANCEL_OPERATION, idempotency_key)
    replayed = idempotency.replay(CANCEL_OPERATION, str(shot_id), key, {})
    if replayed is not None:
        return ShotStatusResponse.model_validate(replayed)
    versions = versions_for(session)
    versions.require(project.id, "shot", shot.id, if_match, label="shot")
    mutations_for(session, principal, controller).cancel_shot(project, shot, idempotency_key=key)
    body = shot_status(session, project.id, shot, versions)
    idempotency.record(
        CANCEL_OPERATION,
        str(shot_id),
        key,
        {},
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


@router.post("/{project_id}/shots/{shot_id}:select-attempt", response_model=ShotDetailResponse)
def select_shot_attempt(
    project_id: UUID,
    shot_id: UUID,
    request: SelectShotAttemptRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ShotDetailResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(SELECT_ATTEMPT_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(SELECT_ATTEMPT_OPERATION, str(shot_id), key, payload)
    if replayed is not None:
        return ShotDetailResponse.model_validate(replayed)
    versions = versions_for(session)
    versions.require(project.id, "shot", shot.id, if_match, label="shot")
    mutations_for(session, principal, controller).select_shot_attempt(
        project, shot, request.attempt_id
    )
    body = shot_detail(session, project.id, shot, versions)
    idempotency.record(
        SELECT_ATTEMPT_OPERATION,
        str(shot_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, body.shot.row_version)
    return body
