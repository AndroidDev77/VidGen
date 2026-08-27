"""Owner-scoped transcript review and single-segment editing."""

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
from apps.api.schemas.transcripts import (
    TranscriptResponse,
    UpdateTranscriptSegmentRequest,
    UpdateTranscriptSegmentResponse,
)
from vidgen.contracts.review import TranscriptSegmentProjection
from vidgen.db.transcription_models import TranscriptSegmentRecord
from vidgen.review.errors import not_found
from vidgen.review.projections import selected_transcript, transcript_projection

router = APIRouter(prefix="/projects", tags=["transcripts"])

EDIT_OPERATION = "transcript-segment:update"


@router.get("/{project_id}/transcript", response_model=TranscriptResponse)
def get_transcript(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, response: Response
) -> TranscriptResponse:
    project = owned_project(session, project_id, principal)
    transcript = selected_transcript(session, project.id)
    body = transcript_projection(session, project.id, transcript, versions_for(session))
    session.commit()
    set_etag(response, body.row_version)
    return body


@router.patch(
    "/{project_id}/transcript/segments/{segment_id}",
    response_model=UpdateTranscriptSegmentResponse,
)
def update_transcript_segment(
    project_id: UUID,
    segment_id: UUID,
    request: UpdateTranscriptSegmentRequest,
    session: SessionDep,
    principal: PrincipalDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> UpdateTranscriptSegmentResponse:
    project = owned_project(session, project_id, principal)
    transcript = selected_transcript(session, project.id)
    segment = session.get(TranscriptSegmentRecord, segment_id)
    if segment is None or segment.transcript_id != transcript.id:
        raise not_found("transcript segment")

    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(EDIT_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replayed = idempotency.replay(EDIT_OPERATION, str(segment_id), key, payload)
    if replayed is not None:
        return UpdateTranscriptSegmentResponse.model_validate(replayed)

    versions = versions_for(session)
    versions.require(
        project.id, "transcript_segment", segment.id, if_match, label="transcript segment"
    )
    updated, invalidation, transcript_version = mutations_for(
        session, principal, controller
    ).edit_transcript_segment(
        project,
        transcript,
        segment,
        text=request.text,
        speaker_label=request.speaker_label,
        confirm_invalidation=request.confirm_invalidation,
    )
    body = UpdateTranscriptSegmentResponse(
        segment=TranscriptSegmentProjection(
            segment_id=updated.id,
            sequence=updated.sequence,
            start_seconds=updated.start_seconds,
            end_seconds=updated.end_seconds,
            text=updated.text,
            speaker_label=updated.speaker_label,
            confidence=updated.confidence,
            edited=True,
            row_version=versions.current(project.id, "transcript_segment", updated.id),
        ),
        transcript_row_version=transcript_version,
        invalidation=invalidation,
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
