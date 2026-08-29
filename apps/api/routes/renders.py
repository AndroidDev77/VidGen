"""Owner-scoped final render retrieval and render-job creation."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from apps.api.routes._common import (
    BlobDep,
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
from apps.api.schemas.renders import RenderResponse, StartRenderRequest, StartRenderResponse
from apps.api.schemas.uploads import DownloadURLResponse
from services.render_execution.commands import completed_render_job
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.models import Asset
from vidgen.review.errors import conflict, not_found
from vidgen.review.projections import current_render, render_is_stale, render_projection

router = APIRouter(prefix="/projects", tags=["renders"])

START_OPERATION = "render:start"


@router.get("/{project_id}/render", response_model=RenderResponse)
def get_render(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, response: Response
) -> RenderResponse:
    project = owned_project(session, project_id, principal)
    render = current_render(session, project.id)
    if render is None:
        raise not_found("render")
    body = render_projection(session, project.id, render, versions_for(session))
    session.commit()
    set_etag(response, body.row_version)
    return body


@router.post("/{project_id}/render:start", response_model=StartRenderResponse)
def start_render(
    project_id: UUID,
    request: StartRenderRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> StartRenderResponse:
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(START_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(START_OPERATION, str(project_id), key, payload)
    if replayed is not None:
        return StartRenderResponse.model_validate(replayed)
    versions = versions_for(session)
    job = mutations_for(session, principal, controller).start_render(project, idempotency_key=key)
    body = StartRenderResponse(render=render_projection(session, project.id, job, versions))
    idempotency.record(
        START_OPERATION,
        str(project_id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


@router.get("/{project_id}/render/download", response_model=DownloadURLResponse)
def download_render(
    project_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    blob_store: BlobDep,
    expires_in_seconds: int = 900,
) -> DownloadURLResponse:
    """Resolve the project's current render to a signed download of the real file.

    The deliverable is whatever T17b actually persisted for the completed
    render - never a placeholder, and never an incomplete, failed or stale
    render dressed up as one. The signed URL is minted at request time by the
    existing download mechanism and is not part of any stored identity.
    """
    project = owned_project(session, project_id, principal)
    render = completed_render_job(session, project.id)
    if render is None or render.final_video_asset_id is None:
        raise conflict(
            ApiErrorCode.RENDER_NOT_VERIFIED,
            "This project has no completed verified render to download.",
        )
    if render_is_stale(session, project.id, render):
        raise conflict(
            ApiErrorCode.RENDER_STALE,
            "This render is stale: its upstream lineage changed after it was produced.",
        )
    asset = session.get(Asset, render.final_video_asset_id)
    if asset is None or asset.project_id != project.id:
        raise not_found("render")
    return DownloadURLResponse(
        asset_id=asset.id,
        url=blob_store.signed_read_url(asset.storage_key, expires_in_seconds),
        expires_in_seconds=expires_in_seconds,
    )
