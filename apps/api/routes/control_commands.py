"""Owner-scoped projections of the T18b durable control plane.

Every asynchronous command the product accepts is a row here, addressable by ID,
with the real workflow identity once it has one. That is what makes the rest of
the API truthful: a route may answer ``202 Accepted`` only because it created a
command that appears in this list and reaches a terminal state on its own.

The projection is deliberately narrower than the row. A command's claim owner,
lease, request hash and trace context are operational state, not owner state,
and no prompt, transcript, script, signed URL or provider payload appears here.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from apps.api.routes._common import (
    PrincipalDep,
    SessionDep,
    owned_project,
)
from apps.api.schemas.control_commands import (
    ControlCommandCollectionResponse,
    ControlCommandResponse,
)
from services.control_plane.commands import ControlPlaneService
from services.control_plane.generation_runs import GenerationRunService, projection

router = APIRouter(prefix="/projects", tags=["control-commands"])


@router.get("/{project_id}/commands", response_model=ControlCommandCollectionResponse)
def list_commands(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ControlCommandCollectionResponse:
    project = owned_project(session, project_id, principal)
    service = ControlPlaneService(session, principal.subject)
    runs = GenerationRunService(session).history(project.id)
    body = ControlCommandCollectionResponse(
        project_id=project.id,
        items=service.list_commands(project),
        generation_runs=[projection(run) for run in runs],
    )
    session.commit()
    return body


@router.get(
    "/{project_id}/commands/{command_id}",
    response_model=ControlCommandResponse,
)
def get_command(
    project_id: UUID, command_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ControlCommandResponse:
    project = owned_project(session, project_id, principal)
    body = ControlCommandResponse(
        command=ControlPlaneService(session, principal.subject).get_command(project, command_id)
    )
    session.commit()
    return body


@router.post(
    "/{project_id}/commands/{command_id}:cancel",
    response_model=ControlCommandResponse,
)
def cancel_command(
    project_id: UUID, command_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ControlCommandResponse:
    """Cancel a command that has not reached a terminal state.

    Cancelling records the owner's decision durably; it does not claim to have
    stopped work that a provider has already been paid for. A command that is
    already terminal is a conflict, not a silent success.
    """
    project = owned_project(session, project_id, principal)
    body = ControlCommandResponse(
        command=ControlPlaneService(session, principal.subject).cancel(project, command_id)
    )
    session.commit()
    return body


@router.post(
    "/{project_id}/commands/{command_id}:retry",
    response_model=ControlCommandResponse,
)
def retry_command(
    project_id: UUID, command_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ControlCommandResponse:
    """Return a failed command to the queue for one further bounded attempt."""
    project = owned_project(session, project_id, principal)
    body = ControlCommandResponse(
        command=ControlPlaneService(session, principal.subject).retry(project, command_id)
    )
    session.commit()
    return body
