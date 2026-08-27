"""Owner-scoped script review, single-segment editing, and version selection."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy import select

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
from apps.api.schemas.scripts import (
    ScriptListResponse,
    ScriptResponse,
    SelectScriptResponse,
    UpdateScriptSegmentRequest,
    UpdateScriptSegmentResponse,
)
from vidgen.contracts.review import ScriptSegmentProjection
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.review.errors import not_found
from vidgen.review.projections import script_projection, script_summary, selected_script

router = APIRouter(prefix="/projects", tags=["scripts"])

EDIT_OPERATION = "script-segment:update"
SELECT_OPERATION = "script:select"


def _owned_script(session: SessionDep, project_id: UUID, script_id: UUID) -> Script:
    script = session.get(Script, script_id)
    if script is None or script.project_id != project_id:
        raise not_found("script")
    return script


@router.get("/{project_id}/scripts", response_model=ScriptListResponse)
def list_scripts(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ScriptListResponse:
    project = owned_project(session, project_id, principal)
    versions = versions_for(session)
    rows = session.scalars(
        select(Script).where(Script.project_id == project.id).order_by(Script.version)
    ).all()
    body = ScriptListResponse(items=[script_summary(project.id, row, versions) for row in rows])
    session.commit()
    return body


@router.get("/{project_id}/scripts/{script_id}", response_model=ScriptResponse)
def get_script(
    project_id: UUID,
    script_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
) -> ScriptResponse:
    project = owned_project(session, project_id, principal)
    script = _owned_script(session, project.id, script_id)
    body = script_projection(session, project.id, script, versions_for(session))
    session.commit()
    set_etag(response, body.script.row_version)
    return body


@router.get("/{project_id}/script", response_model=ScriptResponse)
def get_selected_script(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, response: Response
) -> ScriptResponse:
    project = owned_project(session, project_id, principal)
    script = selected_script(session, project.id)
    body = script_projection(session, project.id, script, versions_for(session))
    session.commit()
    set_etag(response, body.script.row_version)
    return body


@router.post("/{project_id}/scripts/{script_id}:select", response_model=SelectScriptResponse)
def select_script(
    project_id: UUID,
    script_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> SelectScriptResponse:
    project = owned_project(session, project_id, principal)
    script = _owned_script(session, project.id, script_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(SELECT_OPERATION, idempotency_key)
    replayed = idempotency.replay(SELECT_OPERATION, str(script_id), key, {})
    if replayed is not None:
        return SelectScriptResponse.model_validate(replayed)
    versions = versions_for(session)
    versions.require(project.id, "script", script.id, if_match, label="script")
    selected = mutations_for(session, principal, controller).select_script(project, script)
    body = SelectScriptResponse(script=script_summary(project.id, selected, versions))
    idempotency.record(
        SELECT_OPERATION,
        str(script_id),
        key,
        {},
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    return body


@router.patch(
    "/{project_id}/script-segments/{segment_id}", response_model=UpdateScriptSegmentResponse
)
def update_script_segment(
    project_id: UUID,
    segment_id: UUID,
    request: UpdateScriptSegmentRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> UpdateScriptSegmentResponse:
    project = owned_project(session, project_id, principal)
    segment = session.get(ScriptSegment, segment_id)
    script = session.get(Script, segment.script_id) if segment is not None else None
    if segment is None or script is None or script.project_id != project.id:
        raise not_found("script segment")

    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(EDIT_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(EDIT_OPERATION, str(segment_id), key, payload)
    if replayed is not None:
        return UpdateScriptSegmentResponse.model_validate(replayed)

    versions = versions_for(session)
    versions.require(project.id, "script_segment", segment.id, if_match, label="script segment")
    outcome = mutations_for(session, principal, controller).edit_script_segment(
        project,
        script,
        segment,
        text=request.text,
        visual_gag=request.visual_gag,
        confirm_invalidation=request.confirm_invalidation,
    )
    updated = outcome.segment
    body = UpdateScriptSegmentResponse(
        segment=ScriptSegmentProjection(
            segment_id=updated.id,
            stable_segment_id=updated.stable_segment_id,
            sequence=updated.sequence,
            segment_type=updated.segment_type,
            speaker_kind=updated.speaker_kind,
            speaker_label=updated.anonymous_speaker_label,
            text=updated.text,
            visual_gag=updated.visual_gag,
            joke_annotation_count=len(updated.joke_annotations or []),
            plot_beat_ids=[str(item) for item in (updated.plot_beat_ids or [])],
            word_count=len(updated.text.split()),
            estimated_duration_ms=updated.estimated_duration_ms,
            measured_narration_duration_ms=None,
            locked=updated.locked,
            content_hash=updated.content_hash,
            row_version=versions.current(project.id, "script_segment", updated.id),
        ),
        script=script_summary(project.id, outcome.script, versions),
        created_version=outcome.created_version,
        invalidation=outcome.invalidation,
    )
    idempotency.record(
        EDIT_OPERATION,
        str(segment_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, body.segment.row_version)
    return body
