"""Owner-scoped transcript review and single-segment editing.

An edit preserves the original text and provider provenance, computes the exact
downstream invalidation, and - since T18b - creates the durable command that
actually rebuilds it. Before that command existed, a confirmed edit recorded an
invalidation nothing ever acted on, so a corrected transcript never reached the
script, the narration or the render that depended on it.
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
from apps.api.schemas.transcripts import (
    TranscriptResponse,
    UpdateTranscriptSegmentRequest,
    UpdateTranscriptSegmentResponse,
)
from services.control_plane.commands import ControlPlaneService
from services.control_plane.revisions import plan_revision
from vidgen.contracts.control_commands import (
    ControlCommandTargetType,
    ControlCommandType,
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
    # The edit is durable; the rebuild has to be too. Creating the command in
    # this same transaction is what stops a confirmed invalidation from being a
    # note in a table with no consumer.
    plan = plan_revision(session, project_id=project.id, kind="transcript", source_id=transcript.id)
    command = None
    if invalidation.entries:
        command = (
            ControlPlaneService(session, principal.subject)
            .submit(
                project,
                command_type=ControlCommandType.TRANSCRIPT_REVISION,
                target_type=ControlCommandTargetType.TRANSCRIPT,
                target_id=transcript.id,
                idempotency_key=f"transcript-revision:{key}"[:255],
                payload={"segment_id": str(segment_id), **payload},
                metadata={"entry_stage": plan.entry_stage, "revision_kind": "transcript"},
                entry_stage=plan.entry_stage,
            )
            .command
        )
    body = UpdateTranscriptSegmentResponse(
        rebuild_command_id=command.command_id if command else None,
        rebuild_command_status=command.status.value if command else None,
        rebuild_entry_stage=plan.entry_stage if command else None,
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
