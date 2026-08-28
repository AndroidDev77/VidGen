"""Owner-scoped T20 visual-QA control plane.

Handlers stay thin and never call a provider: a ``:run`` request records a
replayable queued decision that a worker or the CLI picks up, and every read is
a compact projection assembled from persisted rows. Cross-owner and
cross-project IDs return the same ``404`` as a missing one.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from apps.api.routes._common import (
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    idempotency_for,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.visual_qa import (
    VisualQACollectionResponse,
    VisualQADecisionRequest,
    VisualQADecisionResponse,
    VisualQADiagnosticProjection,
    VisualQADimensionProjection,
    VisualQAEvidenceProjection,
    VisualQAEvidenceResponse,
    VisualQARunDetailProjection,
    VisualQARunProjection,
    VisualQARunRequest,
    VisualQARunResponse,
    VisualQASampleProjection,
)
from services.qa.human_review import VisualQAHumanReviewService, require_run
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.visual_qa_models import (
    VisualQAAttempt,
    VisualQAEvidenceRecord,
    VisualQARun,
    VisualQASampleRecord,
)
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.errors import conflict
from vidgen.review.projections import resolve_shot

router = APIRouter(prefix="/projects", tags=["visual-qa"])

QUEUE_NAMESPACE = UUID("2f6e2b52-6c6f-5a5b-9a4b-3c9e5a1d7b40")
RUN_OPERATION = "visual-qa:run"
APPROVE_OPERATION = "visual-qa:approve"
REJECT_OPERATION = "visual-qa:reject"
# Visual QA is a property of one shot, so QA mutations use the shot's row
# version rather than introducing a second concurrency token for the same shot.
QA_RESOURCE = "shot"


def _row_version(session: SessionDep, project_id: UUID, shot_id: UUID) -> int:
    return versions_for(session).current(project_id, QA_RESOURCE, shot_id)


def _attempt(session: SessionDep, run: VisualQARun) -> VisualQAAttempt | None:
    return session.scalar(
        select(VisualQAAttempt)
        .where(VisualQAAttempt.qa_run_id == run.id)
        .order_by(VisualQAAttempt.created_at)
    )


def _projection(session: SessionDep, run: VisualQARun) -> VisualQARunProjection:
    repository = VisualQARepository(session)
    result = repository.canonical_result(run.id)
    attempt = _attempt(session, run)
    review = repository.latest_human_review(run.id)
    diagnostics = (run.deterministic_report or {}).get("metrics", [])
    return VisualQARunProjection(
        qa_run_id=run.id,
        project_id=run.project_id,
        shot_id=run.shot_id,
        target_type=run.target_type,
        status=run.status,
        outcome=run.final_outcome,
        score=run.final_score,
        pass_threshold=run.pass_threshold,
        importance=run.importance,
        hard_failure=bool(run.hard_failure),
        repair_recommendation=run.repair_recommendation,
        repair_codes=list(run.repair_codes or []),
        warning_codes=list(run.warning_codes or []),
        confidence=result.confidence if result is not None else None,
        adjudicated=bool(result is not None and result.adjudication),
        human_review_decision=review.decision if review is not None else None,
        provider=attempt.provider if attempt is not None else "",
        model=attempt.model if attempt is not None else "",
        cost_microusd=run.cost_microusd or 0,
        rubric_version=run.rubric_version,
        threshold_version=run.threshold_version,
        sampling_version=run.sampling_version,
        sample_count=len(repository.samples(run.id)),
        deterministic_warning_count=sum(
            1
            for metric in diagnostics
            if isinstance(metric, dict) and metric.get("outcome") in {"warning", "hard_failure"}
        ),
        row_version=_row_version(session, run.project_id, run.shot_id),
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _sample_projection(row: VisualQASampleRecord) -> VisualQASampleProjection:
    return VisualQASampleProjection(
        sample_id=row.id,
        sequence=row.sequence,
        sample_type=row.sample_type,
        requested_timestamp_us=row.requested_timestamp_us,
        actual_timestamp_us=row.actual_timestamp_us,
        shot_relative_timestamp_us=row.shot_relative_timestamp_us,
        frame_asset_id=row.frame_asset_id,
        frame_sha256=row.frame_sha256,
        selection_reason=row.selection_reason,
        contact_sheet_position=row.contact_sheet_position,
    )


def _detail(session: SessionDep, run: VisualQARun) -> VisualQARunDetailProjection:
    repository = VisualQARepository(session)
    result = repository.canonical_result(run.id)
    dimensions = [
        VisualQADimensionProjection(
            dimension=str(item.get("dimension", "")),
            applicable=bool(item.get("applicable", True)),
            raw_score=float(item.get("raw_score", 0.0)),
            weight=float(item.get("weight", 0.0)),
            effective_weight=float(item.get("effective_weight", 0.0)),
            weighted_contribution=float(item.get("weighted_contribution", 0.0)),
            confidence=float(item.get("confidence", 0.0)),
            warning_codes=list(item.get("warning_codes", [])),
            hard_failure_codes=list(item.get("hard_failure_codes", [])),
            repair_codes=list(item.get("repair_codes", [])),
            finding_summaries=[
                str(finding.get("summary", "")) for finding in item.get("findings", [])
            ][:8],
        )
        for item in (result.dimension_results if result is not None else [])
    ]
    diagnostics = [
        VisualQADiagnosticProjection(
            code=str(metric.get("code", "")),
            outcome=str(metric.get("outcome", "")),
            diagnostic_code=str(metric.get("diagnostic_code", "")),
            measurement=metric.get("measurement"),
            threshold=metric.get("threshold"),
            evidence_timestamp_us=metric.get("evidence_timestamp_us"),
            repair_code=metric.get("repair_code"),
            message=str(metric.get("message", "")),
        )
        for metric in (run.deterministic_report or {}).get("metrics", [])
        if isinstance(metric, dict)
    ]
    samples = [_sample_projection(row) for row in repository.samples(run.id)]
    references = sorted(
        {
            row.compared_reference_asset_id
            for row in (repository.evidence(result.id) if result is not None else [])
            if row.compared_reference_asset_id is not None
        },
        key=str,
    )
    base = _projection(session, run)
    return VisualQARunDetailProjection(
        **base.model_dump(),
        dimensions=dimensions,
        diagnostics=diagnostics,
        samples=samples,
        compared_reference_asset_ids=references,
        contact_sheet_asset_id=run.contact_sheet_asset_id,
        report_asset_id=run.report_asset_id,
        adjudication=result.adjudication if result is not None else None,
    )


@router.get("/{project_id}/visual-qa", response_model=VisualQACollectionResponse)
def list_project_visual_qa(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> VisualQACollectionResponse:
    project = owned_project(session, project_id, principal)
    runs = VisualQARepository(session).runs_for_project(project.id)
    items = [_projection(session, run) for run in runs]
    session.commit()
    return VisualQACollectionResponse(project_id=project.id, items=items)


@router.get("/{project_id}/shots/{shot_id}/visual-qa", response_model=VisualQACollectionResponse)
def list_shot_visual_qa(
    project_id: UUID, shot_id: UUID, session: SessionDep, principal: PrincipalDep
) -> VisualQACollectionResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    items = [
        _projection(session, run)
        for run in VisualQARepository(session).runs_for_shot(project.id, shot.id)
    ]
    session.commit()
    return VisualQACollectionResponse(project_id=project.id, items=items)


@router.get(
    "/{project_id}/shots/{shot_id}/visual-qa/{qa_run_id}",
    response_model=VisualQARunDetailProjection,
)
def get_visual_qa_run(
    project_id: UUID,
    shot_id: UUID,
    qa_run_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
) -> VisualQARunDetailProjection:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    run = require_run(session, project.id, shot.id, qa_run_id)
    body = _detail(session, run)
    session.commit()
    set_etag(response, body.row_version)
    return body


@router.get(
    "/{project_id}/shots/{shot_id}/visual-qa/{qa_run_id}/evidence",
    response_model=VisualQAEvidenceResponse,
)
def get_visual_qa_evidence(
    project_id: UUID,
    shot_id: UUID,
    qa_run_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> VisualQAEvidenceResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    run = require_run(session, project.id, shot.id, qa_run_id)
    repository = VisualQARepository(session)
    result = repository.canonical_result(run.id)
    positions = {row.id: row.contact_sheet_position for row in repository.samples(run.id)}
    rows: list[VisualQAEvidenceRecord] = (
        repository.evidence(result.id) if result is not None else []
    )
    body = VisualQAEvidenceResponse(
        qa_run_id=run.id,
        items=[
            VisualQAEvidenceProjection(
                evidence_id=row.id,
                finding_id=row.finding_id,
                evidence_type=row.evidence_type,
                sample_id=row.sample_id,
                frame_asset_id=row.frame_asset_id,
                shot_relative_timestamp_us=row.shot_relative_timestamp_us,
                source_relative_timestamp_us=row.source_relative_timestamp_us,
                contact_sheet_position=positions.get(row.sample_id)
                if row.sample_id is not None
                else None,
                bounding_box=row.bounding_box,
                compared_reference_asset_id=row.compared_reference_asset_id,
                confidence=row.confidence,
                explanation=row.explanation,
            )
            for row in rows
        ],
        samples=[_sample_projection(row) for row in repository.samples(run.id)],
    )
    session.commit()
    return body


def _queue(
    *,
    project_id: UUID,
    shot_id: UUID | None,
    request: VisualQARunRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> VisualQARunResponse:
    project = owned_project(session, project_id, principal)
    resolved_shot = resolve_shot(session, project.id, shot_id) if shot_id is not None else None
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(RUN_OPERATION, idempotency_key)
    resource_key = str(resolved_shot.id if resolved_shot is not None else project.id)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(RUN_OPERATION, resource_key, key, payload)
    if replay is not None:
        return VisualQARunResponse.model_validate(replay)
    versions = versions_for(session)
    expected = (
        versions.require(project.id, QA_RESOURCE, resolved_shot.id, if_match, label="shot")
        if resolved_shot is not None
        else _project_precondition(session, project.id, if_match)
    )
    body = VisualQARunResponse(
        status="queued",
        project_id=project.id,
        shot_id=resolved_shot.id if resolved_shot is not None else None,
        targets=list(request.targets),
        resource_id=uuid5(QUEUE_NAMESPACE, f"{resource_key}:{RUN_OPERATION}:{key}"),
        row_version=expected,
    )
    idempotency.record(
        RUN_OPERATION,
        resource_key,
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


def _project_precondition(session: SessionDep, project_id: UUID, if_match: str | None) -> int:
    if not if_match:
        raise conflict(ApiErrorCode.PRECONDITION_REQUIRED, "If-Match is required")
    return versions_for(session).require(
        project_id, "project", project_id, if_match, label="project"
    )


@router.post(
    "/{project_id}/visual-qa:run",
    response_model=VisualQARunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_project_visual_qa(
    project_id: UUID,
    request: VisualQARunRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> VisualQARunResponse:
    return _queue(
        project_id=project_id,
        shot_id=None,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/shots/{shot_id}/visual-qa:run",
    response_model=VisualQARunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_shot_visual_qa(
    project_id: UUID,
    shot_id: UUID,
    request: VisualQARunRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> VisualQARunResponse:
    return _queue(
        project_id=project_id,
        shot_id=shot_id,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


def _decide(
    *,
    project_id: UUID,
    shot_id: UUID,
    qa_run_id: UUID,
    decision: str,
    operation: str,
    request: VisualQADecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> VisualQADecisionResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    run = require_run(session, project.id, shot.id, qa_run_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(operation, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(operation, str(qa_run_id), key, payload)
    if replay is not None:
        return VisualQADecisionResponse.model_validate(replay)
    versions = versions_for(session)
    expected = versions.require(project.id, QA_RESOURCE, shot.id, if_match, label="shot")
    outcome = VisualQAHumanReviewService(session, principal.subject).decide(
        run,
        decision=decision,
        reason=request.reason,
        row_version=expected,
        idempotency_key=key,
    )
    new_version = versions.bump(project.id, QA_RESOURCE, shot.id, expected=expected)
    body = VisualQADecisionResponse(
        qa_run_id=run.id,
        review_id=outcome.review_id,
        decision=decision,  # type: ignore[arg-type]
        resulting_gate=outcome.resulting_gate,
        row_version=new_version,
    )
    idempotency.record(
        operation, str(qa_run_id), key, payload, status.HTTP_200_OK, body.model_dump(mode="json")
    )
    session.commit()
    set_etag(response, new_version)
    return body


@router.post(
    "/{project_id}/shots/{shot_id}/visual-qa/{qa_run_id}:approve",
    response_model=VisualQADecisionResponse,
)
def approve_visual_qa(
    project_id: UUID,
    shot_id: UUID,
    qa_run_id: UUID,
    request: VisualQADecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> VisualQADecisionResponse:
    return _decide(
        project_id=project_id,
        shot_id=shot_id,
        qa_run_id=qa_run_id,
        decision="approved",
        operation=APPROVE_OPERATION,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/shots/{shot_id}/visual-qa/{qa_run_id}:reject",
    response_model=VisualQADecisionResponse,
)
def reject_visual_qa(
    project_id: UUID,
    shot_id: UUID,
    qa_run_id: UUID,
    request: VisualQADecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> VisualQADecisionResponse:
    return _decide(
        project_id=project_id,
        shot_id=shot_id,
        qa_run_id=qa_run_id,
        decision="rejected",
        operation=REJECT_OPERATION,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )
