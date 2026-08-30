"""Owner-scoped T25 publication control plane.

The handlers stay thin and never upload anything: ``:start`` and ``:resume``
hand a compact ID-only message to the publisher workflow, and everything a
reader sees is a bounded projection assembled from persisted rows. Cross-owner
and cross-project IDs return the same ``404`` as a missing one.

Two actions run inline because they are single, fast YouTube requests whose
answer the user needs immediately: ``:cancel`` releases a resumable session that
has not produced a video, and ``:visibility`` applies an explicit visibility
decision and reports the privacy YouTube actually returned.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import ValidationError

from apps.api.routes._common import (
    BlobDep,
    ControllerDep,
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    idempotency_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.routes.youtube_connections import keyring_for
from apps.api.schemas.publications import (
    PublicationAssetProjection,
    PublicationAttemptProjection,
    PublicationCancelRequest,
    PublicationCollectionResponse,
    PublicationCreateRequest,
    PublicationDetailProjection,
    PublicationFailureProjection,
    PublicationGateProjection,
    PublicationMetadataRequest,
    PublicationProjection,
    PublicationStartRequest,
    PublicationVisibilityRequest,
)
from apps.api.settings import APISettings
from services.publisher.commands import PublisherCommandOptions, build_pipeline
from services.publisher.eligibility import PublicationEligibilityError
from services.publisher.metadata import PublicationMetadataError
from services.publisher.oauth import OAuthFlowError
from services.publisher.pipeline import PublicationError, PublicationPipeline
from services.publisher.projections import attempt_projections
from vidgen.contracts.publication import (
    PrivacyState,
    PublicationGate,
    PublicationMetadata,
    PublicationResult,
    PublicationStatus,
)
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.publication_models import PublicationAsset, PublicationRun
from vidgen.db.publication_repository import PublicationStateError
from vidgen.review.errors import conflict, not_found

router = APIRouter(prefix="/projects", tags=["publications"])

PUBLICATION_RESOURCE = "project"
CREATE_OPERATION = "publication:create"
START_OPERATION = "publication:start"
RESUME_OPERATION = "publication:resume"
CANCEL_OPERATION = "publication:cancel"
VISIBILITY_OPERATION = "publication:visibility"
DRAFT_OPERATION = "publication:draft"

#: Statuses from which a cancel is meaningful: nothing exists on YouTube yet.
CANCELLABLE_STATUSES = frozenset(
    {
        PublicationStatus.DRAFT.value,
        PublicationStatus.AUTHORIZATION_REQUIRED.value,
        PublicationStatus.READY.value,
        PublicationStatus.UPLOAD_INITIALIZING.value,
        PublicationStatus.UPLOADING.value,
    }
)


def _precondition(session: SessionDep, project_id: UUID, if_match: str | None) -> int:
    if not if_match:
        raise conflict(ApiErrorCode.PRECONDITION_REQUIRED, "If-Match is required")
    return versions_for(session).require(
        project_id, PUBLICATION_RESOURCE, project_id, if_match, label="project"
    )


def _row_version(session: SessionDep, project_id: UUID) -> int:
    return versions_for(session).current(project_id, PUBLICATION_RESOURCE, project_id)


def _pipeline(session: SessionDep, blob: BlobDep, settings: APISettings) -> PublicationPipeline:
    return build_pipeline(
        session,
        blob,
        PublisherCommandOptions(
            provider=settings.youtube_provider,
            chunk_bytes=settings.youtube_upload_chunk_bytes,
        ),
        keyring=keyring_for(settings),
    )


def _failure_projection(result: PublicationResult) -> PublicationFailureProjection | None:
    if result.failure is None:
        return None
    return PublicationFailureProjection(
        code=result.failure.code.value,
        summary=result.failure.summary,
        retryable=result.failure.retryable,
        http_status=result.failure.http_status,
        remediation=result.failure.remediation,
    )


def _metadata_projection(run: PublicationRun) -> PublicationMetadataRequest | None:
    if not run.draft_metadata:
        return None
    draft = PublicationMetadata.model_validate(run.draft_metadata)
    return PublicationMetadataRequest(
        title=draft.title,
        description=draft.description,
        tags=list(draft.tags),
        category_id=draft.category_id,
        default_language=draft.default_language,
        caption_language=draft.caption_language,
        caption_track_name=draft.caption_track_name,
        made_for_kids=draft.made_for_kids,
        contains_synthetic_media=draft.contains_synthetic_media,
        embeddable=draft.embeddable,
        notify_subscribers=draft.notify_subscribers,
        requested_privacy=draft.requested_privacy.value,
        scheduled_publish_at=draft.scheduled_publish_at,
    )


def _projection(
    run: PublicationRun, result: PublicationResult, row_version: int
) -> PublicationProjection:
    return PublicationProjection(
        publication_id=result.publication_run_id,
        project_id=result.project_id,
        connection_id=result.connection_id,
        channel_id=result.channel_id,
        final_render_asset_id=result.final_render_asset_id,
        final_editorial_run_id=result.final_editorial_run_id,
        approval_id=result.approval_id,
        publication_identity=result.publication_identity,
        metadata_version=result.metadata_version,
        status=result.status.value,
        phase=result.phase.value,
        video_id=result.video_id,
        video_url=result.video_url,
        total_bytes=result.total_bytes,
        confirmed_offset=result.confirmed_offset,
        processing_state=result.processing_state.value if result.processing_state else None,
        caption_status=result.caption_status.value if result.caption_status else None,
        caption_track_id=result.caption_track_id,
        thumbnail_status=result.thumbnail_status.value if result.thumbnail_status else None,
        requested_privacy=result.requested_privacy.value,
        actual_privacy=result.actual_privacy.value if result.actual_privacy else None,
        scheduled_publish_at=result.scheduled_publish_at,
        contains_synthetic_media=result.contains_synthetic_media,
        made_for_kids=result.made_for_kids,
        notify_subscribers=result.notify_subscribers,
        quota_units=result.quota_units,
        capability_profile_version=result.capability_profile_version,
        publisher_version=result.publisher_version,
        gate_version=run.gate_version or "",
        render_identity=run.render_identity or "",
        metadata=_metadata_projection(run),
        failure=_failure_projection(result),
        row_version=row_version,
        created_at=result.created_at,
        updated_at=result.updated_at,
    )


def _gate_projection(gate: PublicationGate, row_version: int) -> PublicationGateProjection:
    return PublicationGateProjection(
        project_id=gate.project_id,
        allowed=gate.allowed,
        final_render_asset_id=gate.final_render_asset_id,
        final_editorial_run_id=gate.final_editorial_run_id,
        approval_id=gate.approval_id,
        caption_asset_id=gate.caption_asset_id,
        gate_version=gate.gate_version,
        failures=[
            PublicationFailureProjection(
                code=failure.code.value,
                summary=failure.summary,
                retryable=failure.retryable,
                http_status=failure.http_status,
                remediation=failure.remediation,
            )
            for failure in gate.failures
        ],
        warnings=[warning.summary for warning in gate.warnings],
        row_version=row_version,
    )


def _asset_projection(row: PublicationAsset) -> PublicationAssetProjection:
    return PublicationAssetProjection(
        kind=row.kind,
        status=row.status,
        local_asset_id=row.local_asset_id,
        provider_resource_id=row.provider_resource_id or "",
        language=row.language or "",
        name=row.name or "",
        byte_size=int(row.byte_size or 0),
        error_code=row.error_code,
        error_summary=row.error_summary,
    )


def _first_validation_message(error: ValidationError) -> str:
    """The first contract failure, rendered without a traceback or input echo."""
    for item in error.errors():
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        if message:
            return message[:500]
    return "The publication metadata is not valid."


def _require_run(
    pipeline: PublicationPipeline, publication_id: UUID, project_id: UUID, owner: str
) -> PublicationRun:
    run = pipeline.repository.owned_run(publication_id, project_id, owner)
    if run is None:
        raise not_found("publication")
    return run


# -- reads ---------------------------------------------------------------------
@router.get("/{project_id}/publications", response_model=PublicationCollectionResponse)
def list_publications(
    project_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
) -> PublicationCollectionResponse:
    project = owned_project(session, project_id, principal)
    pipeline = _pipeline(session, blob, settings)
    version = _row_version(session, project.id)
    connections = pipeline.repository.connections_for_owner(principal.subject)
    gate, _ = pipeline.eligibility.evaluate(
        project_id=project.id,
        owner_subject=principal.subject,
        connection_id=connections[0].id if connections else None,
    )
    items = [
        _projection(run, pipeline.project(run), version)
        for run in pipeline.repository.runs_for_project(project.id)
    ]
    session.commit()
    return PublicationCollectionResponse(
        project_id=project.id, items=items, gate=_gate_projection(gate, version)
    )


@router.get(
    "/{project_id}/publications/{publication_id}",
    response_model=PublicationDetailProjection,
)
def get_publication(
    project_id: UUID,
    publication_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    response: Response,
) -> PublicationDetailProjection:
    project = owned_project(session, project_id, principal)
    pipeline = _pipeline(session, blob, settings)
    run = _require_run(pipeline, publication_id, project.id, principal.subject)
    version = _row_version(session, project.id)
    base = _projection(run, pipeline.project(run), version)
    detail = PublicationDetailProjection(
        **base.model_dump(),
        assets=[_asset_projection(row) for row in pipeline.repository.assets_for(run.id)],
        attempts=[
            PublicationAttemptProjection(
                attempt_id=attempt.attempt_id,
                operation=attempt.operation,
                status=attempt.status,
                provider=attempt.provider,
                latency_ms=attempt.latency_ms,
                quota_units=attempt.quota_units,
                failure_code=attempt.failure.code.value if attempt.failure else None,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
            )
            for attempt in attempt_projections(session, run)
        ],
    )
    session.commit()
    set_etag(response, version)
    return detail


# -- mutations -----------------------------------------------------------------
@router.post(
    "/{project_id}/publications",
    response_model=PublicationProjection,
    status_code=status.HTTP_201_CREATED,
)
def create_publication(
    project_id: UUID,
    request: PublicationCreateRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Create, or return, the publication draft for this render and channel."""
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CREATE_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(CREATE_OPERATION, str(project.id), key, payload)
    if replay is not None:
        return PublicationProjection.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    pipeline = _pipeline(session, blob, settings)
    try:
        metadata = (
            PublicationMetadata.model_validate(
                {**request.metadata.model_dump(), "metadata_version": 1}
            )
            if request.metadata is not None
            else None
        )
        run = pipeline.create_draft(
            project_id=project.id,
            owner_subject=principal.subject,
            connection_id=request.connection_id,
            idempotency_key=key,
            thumbnail_asset_id=request.thumbnail_asset_id,
            metadata=metadata,
        )
    except PublicationEligibilityError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, error.gate.failures[0].summary) from error
    except ValidationError as error:
        # A contract rule the request schema cannot express on its own, such as
        # a scheduled time on a video that is not going out public.
        raise conflict(ApiErrorCode.VALIDATION_FAILED, _first_validation_message(error)) from error
    except (PublicationMetadataError, PublicationError) as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    body = _projection(run, pipeline.project(run), expected)
    idempotency.record(
        CREATE_OPERATION,
        str(project.id),
        key,
        payload,
        status.HTTP_201_CREATED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.patch(
    "/{project_id}/publications/{publication_id}",
    response_model=PublicationProjection,
)
def update_publication_draft(
    project_id: UUID,
    publication_id: UUID,
    request: PublicationMetadataRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Edit this publication's draft in place.

    Deliberately a PATCH on the existing publication rather than another POST
    to the collection: a create call carries the metadata into the publication
    *identity*, so saving an edit that way would mint a new identity and a
    second ``publication_runs`` row for every keystroke the user saved. Editing
    versions the draft on the row that already exists, and after upload the
    same version is what ``videos.update`` writes to the existing video.
    """
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(DRAFT_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(DRAFT_OPERATION, str(publication_id), key, payload)
    if replay is not None:
        return PublicationProjection.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    pipeline = _pipeline(session, blob, settings)
    run = _require_run(pipeline, publication_id, project.id, principal.subject)
    try:
        edited = PublicationMetadata.model_validate(
            {**payload, "metadata_version": run.metadata_version}
        )
        pipeline.update_draft(run, edited)
    except ValidationError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, _first_validation_message(error)) from error
    except (PublicationMetadataError, PublicationStateError) as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    body = _projection(run, pipeline.project(run), expected)
    idempotency.record(
        DRAFT_OPERATION,
        str(publication_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


def _dispatch(controller: ControllerDep, run: PublicationRun, trace_context: dict[str, str]) -> str:
    """Hand the workflow a compact ID-only message and return its workflow ID."""
    from vidgen.contracts.publication import PublicationActivityInput

    workflow_id, _ = controller.start_publication(
        PublicationActivityInput(
            project_id=run.project_id,
            publication_run_id=run.id,
            connection_id=run.connection_id,
            final_render_asset_id=run.final_render_asset_id,
            idempotency_key=run.idempotency_key,
            trace_context=trace_context,
        )
    )
    return workflow_id


@router.post(
    "/{project_id}/publications/{publication_id}:start",
    response_model=PublicationProjection,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_publication(
    project_id: UUID,
    publication_id: UUID,
    request: PublicationStartRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Ask the publisher workflow to upload this publication."""
    return _start_or_resume(
        START_OPERATION,
        project_id,
        publication_id,
        request.model_dump(mode="json"),
        session,
        principal,
        blob,
        settings,
        controller,
        response,
        if_match,
        idempotency_key,
    )


@router.post(
    "/{project_id}/publications/{publication_id}:resume",
    response_model=PublicationProjection,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_publication(
    project_id: UUID,
    publication_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Continue an interrupted upload from its server-confirmed offset."""
    return _start_or_resume(
        RESUME_OPERATION,
        project_id,
        publication_id,
        {"resume": True},
        session,
        principal,
        blob,
        settings,
        controller,
        response,
        if_match,
        idempotency_key,
    )


def _start_or_resume(
    operation: str,
    project_id: UUID,
    publication_id: UUID,
    payload: dict[str, object],
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: APISettings,
    controller: ControllerDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> PublicationProjection:
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(operation, idempotency_key)
    replay = idempotency.replay(operation, str(publication_id), key, payload)
    if replay is not None:
        return PublicationProjection.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    pipeline = _pipeline(session, blob, settings)
    run = _require_run(pipeline, publication_id, project.id, principal.subject)
    if run.status == PublicationStatus.HUMAN_REVIEW_REQUIRED.value:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "this publication is held for review because YouTube's outcome could not be "
            "established; resolve it before starting another upload",
        )
    if run.status in {PublicationStatus.CANCELLED.value, PublicationStatus.FAILED.value}:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "this publication has finished; create a new one for the current render",
        )
    _dispatch(controller, run, {})
    body = _projection(run, pipeline.project(run), expected)
    idempotency.record(
        operation,
        str(publication_id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.post(
    "/{project_id}/publications/{publication_id}:cancel",
    response_model=PublicationProjection,
)
async def cancel_publication(
    project_id: UUID,
    publication_id: UUID,
    request: PublicationCancelRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    controller: ControllerDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Cancel before a video exists. An uploaded video is never deleted here."""
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CANCEL_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(CANCEL_OPERATION, str(publication_id), key, payload)
    if replay is not None:
        return PublicationProjection.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    pipeline = _pipeline(session, blob, settings)
    run = _require_run(pipeline, publication_id, project.id, principal.subject)
    if run.status not in CANCELLABLE_STATUSES or run.video_id:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "a publication can only be cancelled before YouTube has created the video",
        )
    try:
        await pipeline.cancel(run)
    except (PublicationError, PublicationStateError) as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    from vidgen.review.workflow_control import publication_workflow_id

    try:
        controller.cancel_publication(publication_workflow_id(run.id))
    except Exception:  # the local cancel already happened and is authoritative
        pass
    body = _projection(run, pipeline.project(run), expected)
    idempotency.record(
        CANCEL_OPERATION,
        str(publication_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.post(
    "/{project_id}/publications/{publication_id}:visibility",
    response_model=PublicationProjection,
)
async def change_visibility(
    project_id: UUID,
    publication_id: UUID,
    request: PublicationVisibilityRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    settings: SettingsDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> PublicationProjection:
    """Apply an explicit visibility decision and report what YouTube returned."""
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(VISIBILITY_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(VISIBILITY_OPERATION, str(publication_id), key, payload)
    if replay is not None:
        return PublicationProjection.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    pipeline = _pipeline(session, blob, settings)
    run = _require_run(pipeline, publication_id, project.id, principal.subject)
    try:
        await pipeline.apply_visibility(
            run,
            privacy=PrivacyState(request.privacy),
            actor=principal.subject,
            scheduled_publish_at=request.scheduled_publish_at,
            notify_subscribers=request.notify_subscribers,
        )
    except PublicationEligibilityError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, error.gate.failures[0].summary) from error
    except (PublicationError, PublicationMetadataError, OAuthFlowError) as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, str(error)) from error
    body = _projection(run, pipeline.project(run), expected)
    idempotency.record(
        VISIBILITY_OPERATION,
        str(publication_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body
