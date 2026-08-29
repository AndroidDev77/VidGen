"""Owner-scoped T22 final editorial-QA control plane.

Handlers stay thin and never call a provider: a ``:run`` request records a
replayable queued decision that a worker or the CLI picks up, and every read is
a compact projection assembled from persisted rows and the immutable report.
Cross-owner and cross-project IDs return the same ``404`` as a missing one.

Two actions are deliberately absent. There is no endpoint that marks a
deterministic hard failure as passed, and no endpoint that starts a paid
generation call: routing a confirmed finding hands it to the stage that already
owns that repair.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from apps.api.routes._common import (
    BlobDep,
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    idempotency_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.final_editorial import (
    FinalCompletionGateProjection,
    FinalEditorialCancelRequest,
    FinalEditorialCollectionResponse,
    FinalEditorialRemediationRequest,
    FinalEditorialRemediationResponse,
    FinalEditorialReviewRequest,
    FinalEditorialReviewResponse,
    FinalEditorialRunDetailProjection,
    FinalEditorialRunRequest,
    FinalEditorialRunResponse,
)
from services.qa.final_human_review import (
    FinalEditorialHumanReviewService,
    report_payload,
    require_run,
)
from services.qa.final_projections import (
    FINAL_QA_RESOURCE,
    detail_projection,
    row_version,
    run_projection,
)
from services.qa.final_rubric import GATE_VERSION
from vidgen.contracts.final_editorial import FinalQAStatus, FinalRemediationTarget
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.final_editorial_repository import FinalEditorialRepository
from vidgen.db.models import RenderJob
from vidgen.review.errors import conflict

router = APIRouter(prefix="/projects", tags=["final-editorial-qa"])

QUEUE_NAMESPACE = UUID("b1d6f0a4-7c39-5f2e-8a41-6d0b93c2ea57")
RUN_OPERATION = "final-editorial:run"
CANCEL_OPERATION = "final-editorial:cancel"
REVIEW_OPERATION = "final-editorial:review"
REMEDIATE_OPERATION = "final-editorial:remediate"
#: Phases before any paid provider request. Cancelling here spends nothing.
CANCELLABLE_STATUSES = frozenset(
    {
        FinalQAStatus.FINAL_QA_QUEUED.value,
        FinalQAStatus.FINAL_QA_VALIDATING_INPUTS.value,
        FinalQAStatus.FINAL_QA_CHECKING_MEDIA.value,
        FinalQAStatus.FINAL_QA_CHECKING_CAPTIONS.value,
    }
)


# --- reads ---------------------------------------------------------------
@router.get("/{project_id}/final-qa", response_model=FinalEditorialCollectionResponse)
def list_final_editorial_runs(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, blob: BlobDep
) -> FinalEditorialCollectionResponse:
    project = owned_project(session, project_id, principal)
    repository = FinalEditorialRepository(session)
    items = [
        run_projection(session, run, report_payload(blob, session, run))
        for run in repository.runs_for_project(project.id)
    ]
    session.commit()
    return FinalEditorialCollectionResponse(project_id=project.id, items=items)


# Declared before the parameterised run route: FastAPI matches in declaration
# order, and a literal segment must win over a UUID path parameter.
@router.get("/{project_id}/final-qa/gate", response_model=FinalCompletionGateProjection)
def get_completion_gate(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> FinalCompletionGateProjection:
    """The workflow's own gate answer, so the UI never invents completion."""
    project = owned_project(session, project_id, principal)
    repository = FinalEditorialRepository(session)
    job = session.scalar(
        select(RenderJob)
        .where(RenderJob.project_id == project.id, RenderJob.selected.is_(True))
        .order_by(RenderJob.created_at.desc())
    )
    render_asset_id = job.final_video_asset_id if job is not None else None
    allowed, reason = repository.completion_gate(project.id, render_asset_id)
    run = repository.selected_run(project.id)
    session.commit()
    return FinalCompletionGateProjection(
        project_id=project.id,
        final_editorial_run_id=run.id if run is not None else None,
        final_render_asset_id=render_asset_id,
        decision=run.final_decision if run is not None else None,  # type: ignore[arg-type]
        allowed=allowed,
        reason=reason,
        blocking_finding_count=run.blocking_finding_count if run is not None else 0,
        review_finding_count=run.review_finding_count if run is not None else 0,
        deterministic_failure_count=run.deterministic_failure_count if run is not None else 0,
        gate_version=GATE_VERSION,
        row_version=row_version(session, project.id),
    )


@router.get(
    "/{project_id}/final-qa/{final_editorial_run_id}",
    response_model=FinalEditorialRunDetailProjection,
)
def get_final_editorial_run(
    project_id: UUID,
    final_editorial_run_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    response: Response,
) -> FinalEditorialRunDetailProjection:
    project = owned_project(session, project_id, principal)
    run = require_run(session, project.id, final_editorial_run_id)
    body = detail_projection(session, blob, run)
    session.commit()
    set_etag(response, body.row_version)
    return body


# --- mutations -----------------------------------------------------------
def _precondition(session: SessionDep, project_id: UUID, if_match: str | None) -> int:
    if not if_match:
        raise conflict(ApiErrorCode.PRECONDITION_REQUIRED, "If-Match is required")
    return versions_for(session).require(
        project_id, FINAL_QA_RESOURCE, project_id, if_match, label="project"
    )


@router.post(
    "/{project_id}/final-qa:run",
    response_model=FinalEditorialRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_final_editorial_qa(
    project_id: UUID,
    request: FinalEditorialRunRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> FinalEditorialRunResponse:
    project = owned_project(session, project_id, principal)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(RUN_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(RUN_OPERATION, str(project.id), key, payload)
    if replay is not None:
        return FinalEditorialRunResponse.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    job = session.scalar(
        select(RenderJob)
        .where(RenderJob.project_id == project.id, RenderJob.selected.is_(True))
        .order_by(RenderJob.created_at.desc())
    )
    body = FinalEditorialRunResponse(
        status="queued",
        project_id=project.id,
        final_render_asset_id=job.final_video_asset_id if job is not None else None,
        provider=request.provider,
        resource_id=uuid5(QUEUE_NAMESPACE, f"{project.id}:{RUN_OPERATION}:{key}"),
        row_version=expected,
    )
    idempotency.record(
        RUN_OPERATION,
        str(project.id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.post(
    "/{project_id}/final-qa/{final_editorial_run_id}:cancel",
    response_model=FinalEditorialRunResponse,
)
def cancel_final_editorial_qa(
    project_id: UUID,
    final_editorial_run_id: UUID,
    request: FinalEditorialCancelRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> FinalEditorialRunResponse:
    """Cancel before a paid analysis call. Afterwards the cost is already spent."""
    project = owned_project(session, project_id, principal)
    run = require_run(session, project.id, final_editorial_run_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(CANCEL_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(CANCEL_OPERATION, str(run.id), key, payload)
    if replay is not None:
        return FinalEditorialRunResponse.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    if run.status not in CANCELLABLE_STATUSES:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "final QA can only be cancelled before its paid editorial analysis",
        )
    run.status = FinalQAStatus.FINAL_QA_FAILED.value
    run.error_code = "cancelled"
    body = FinalEditorialRunResponse(
        status="cancelled",
        project_id=project.id,
        final_render_asset_id=run.final_render_asset_id,
        resource_id=uuid5(QUEUE_NAMESPACE, f"{run.id}:{CANCEL_OPERATION}:{key}"),
        row_version=expected,
    )
    idempotency.record(
        CANCEL_OPERATION,
        str(run.id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.post(
    "/{project_id}/final-qa/{final_editorial_run_id}:review",
    response_model=FinalEditorialReviewResponse,
)
def resolve_final_editorial_review(
    project_id: UUID,
    final_editorial_run_id: UUID,
    request: FinalEditorialReviewRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> FinalEditorialReviewResponse:
    """Resolve one eligible semantic review finding. Never a measured failure."""
    project = owned_project(session, project_id, principal)
    run = require_run(session, project.id, final_editorial_run_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(REVIEW_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(REVIEW_OPERATION, str(run.id), key, payload)
    if replay is not None:
        return FinalEditorialReviewResponse.model_validate(replay)
    versions = versions_for(session)
    expected = _precondition(session, project.id, if_match)
    outcome = FinalEditorialHumanReviewService(session, blob, principal.subject).decide(
        run,
        finding_id=request.finding_id,
        decision=request.decision,
        reason_code=request.reason_code,
        reason=request.reason,
        row_version=expected,
        idempotency_key=key,
    )
    new_version = versions.bump(project.id, FINAL_QA_RESOURCE, project.id, expected=expected)
    body = FinalEditorialReviewResponse(
        final_editorial_run_id=run.id,
        review_id=outcome.review_id,
        finding_id=outcome.finding_id,
        decision=request.decision,
        resulting_gate=outcome.resulting_gate,  # type: ignore[arg-type]
        row_version=new_version,
    )
    idempotency.record(
        REVIEW_OPERATION,
        str(run.id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, new_version)
    return body


@router.post(
    "/{project_id}/final-qa/{final_editorial_run_id}:remediate",
    response_model=FinalEditorialRemediationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def route_final_editorial_remediation(
    project_id: UUID,
    final_editorial_run_id: UUID,
    request: FinalEditorialRemediationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    blob: BlobDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> FinalEditorialRemediationResponse:
    """Hand confirmed findings to the existing stage that owns their repair."""
    project = owned_project(session, project_id, principal)
    run = require_run(session, project.id, final_editorial_run_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(REMEDIATE_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(REMEDIATE_OPERATION, str(run.id), key, payload)
    if replay is not None:
        return FinalEditorialRemediationResponse.model_validate(replay)
    expected = _precondition(session, project.id, if_match)
    try:
        target = FinalRemediationTarget(request.target)
    except ValueError as error:
        raise conflict(ApiErrorCode.VALIDATION_FAILED, "unknown remediation target") from error
    report = report_payload(blob, session, run)
    known = {
        UUID(str(item["finding_id"]))
        for item in report.get("findings", [])
        if isinstance(item, dict) and item.get("finding_id")
    }
    unknown = [finding for finding in request.finding_ids if finding not in known]
    if unknown:
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "a remediation route may only reference findings from this report",
        )
    body = FinalEditorialRemediationResponse(
        final_editorial_run_id=run.id,
        target=target.value,
        routed_finding_ids=list(request.finding_ids),
        # Any change to a selected input invalidates this render, so a new T17
        # render and a new T22 run are required before the project can complete.
        requires_new_render=target is not FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW,
        resource_id=uuid5(QUEUE_NAMESPACE, f"{run.id}:{REMEDIATE_OPERATION}:{key}"),
        row_version=expected,
    )
    idempotency.record(
        REMEDIATE_OPERATION,
        str(run.id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body
