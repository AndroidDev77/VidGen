"""Owner-scoped render approval.

Approval records a decision; it never publishes the video. T25 owns publication.
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
from apps.api.schemas.reviews import ApproveRenderRequest, ApproveRenderResponse
from vidgen.contracts.review import ApiErrorCode, RenderApprovalProjection
from vidgen.review.errors import conflict, not_found
from vidgen.review.projections import current_render, render_projection, utc

router = APIRouter(prefix="/projects", tags=["reviews"])

APPROVE_OPERATION = "review:approve"


@router.post("/{project_id}/review:approve", response_model=ApproveRenderResponse)
def approve_render(
    project_id: UUID,
    request: ApproveRenderRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ApproveRenderResponse:
    project = owned_project(session, project_id, principal)
    render = current_render(session, project.id)
    if render is None:
        raise not_found("render")
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(APPROVE_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(APPROVE_OPERATION, str(project_id), key, payload)
    if replayed is not None:
        return ApproveRenderResponse.model_validate(replayed)

    versions = versions_for(session)
    versions.require(project.id, "render", render.id, if_match, label="render")
    projection = render_projection(session, project.id, render, versions)
    if projection.lineage_hash != request.lineage_hash:
        raise conflict(
            ApiErrorCode.RENDER_STALE,
            "The render lineage changed since you reviewed it. Reload the final review page.",
            current_version=projection.row_version,
        )
    approval = mutations_for(session, principal, controller).approve_render(
        project, render, request.lineage_hash
    )
    body = ApproveRenderResponse(
        approval=RenderApprovalProjection(
            approval_id=approval.id,
            render_job_id=approval.render_job_id,
            approved_by=approval.approved_by,
            approved_at=utc(approval.approved_at) or approval.approved_at,
            lineage_hash=approval.lineage_hash,
            applies_to_current_lineage=True,
        ),
        render=render_projection(session, project.id, render, versions),
    )
    idempotency.record(
        APPROVE_OPERATION,
        str(project_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, body.render.row_version)
    return body
