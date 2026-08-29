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

from typing import Any
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
    FinalCheckProjection,
    FinalCompletionGateProjection,
    FinalDimensionProjection,
    FinalEditorialCancelRequest,
    FinalEditorialCollectionResponse,
    FinalEditorialRemediationRequest,
    FinalEditorialRemediationResponse,
    FinalEditorialReviewRequest,
    FinalEditorialReviewResponse,
    FinalEditorialRunDetailProjection,
    FinalEditorialRunProjection,
    FinalEditorialRunRequest,
    FinalEditorialRunResponse,
    FinalEvidenceProjection,
    FinalFindingProjection,
    FinalMeasurementProjection,
    FinalRemediationProjection,
)
from services.qa.final_human_review import (
    FinalEditorialHumanReviewService,
    report_payload,
    require_run,
)
from services.qa.final_rubric import GATE_VERSION
from vidgen.contracts.final_editorial import FinalQAStatus, FinalRemediationTarget
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.final_editorial_models import FinalEditorialProviderAttempt, FinalEditorialRun
from vidgen.db.final_editorial_repository import FinalEditorialRepository
from vidgen.db.models import RenderJob
from vidgen.review.errors import conflict

router = APIRouter(prefix="/projects", tags=["final-editorial-qa"])

QUEUE_NAMESPACE = UUID("b1d6f0a4-7c39-5f2e-8a41-6d0b93c2ea57")
RUN_OPERATION = "final-editorial:run"
CANCEL_OPERATION = "final-editorial:cancel"
REVIEW_OPERATION = "final-editorial:review"
REMEDIATE_OPERATION = "final-editorial:remediate"
#: Final QA is a property of the project's current render, so its mutations use
#: the project row version rather than introducing a second concurrency token.
FINAL_QA_RESOURCE = "project"
#: Phases before any paid provider request. Cancelling here spends nothing.
CANCELLABLE_STATUSES = frozenset(
    {
        FinalQAStatus.FINAL_QA_QUEUED.value,
        FinalQAStatus.FINAL_QA_VALIDATING_INPUTS.value,
        FinalQAStatus.FINAL_QA_CHECKING_MEDIA.value,
        FinalQAStatus.FINAL_QA_CHECKING_CAPTIONS.value,
    }
)


def _row_version(session: SessionDep, project_id: UUID) -> int:
    return versions_for(session).current(project_id, FINAL_QA_RESOURCE, project_id)


def _attempts(session: SessionDep, run: FinalEditorialRun) -> list[FinalEditorialProviderAttempt]:
    return list(
        session.scalars(
            select(FinalEditorialProviderAttempt)
            .where(FinalEditorialProviderAttempt.final_editorial_run_id == run.id)
            .order_by(FinalEditorialProviderAttempt.created_at)
        )
    )


def _projection(
    session: SessionDep, run: FinalEditorialRun, report: dict[str, Any]
) -> FinalEditorialRunProjection:
    attempts = _attempts(session, run)
    return FinalEditorialRunProjection(
        final_editorial_run_id=run.id,
        project_id=run.project_id,
        final_render_asset_id=run.final_render_asset_id,
        render_manifest_asset_id=run.render_manifest_asset_id,
        render_identity=run.render_identity,
        final_qa_identity=run.final_qa_identity,
        input_hash=run.input_hash,
        configuration_hash=run.configuration_hash,
        report_version=str(report.get("report_version", "")),
        status=run.status,
        phase=run.current_phase,
        decision=run.final_decision,  # type: ignore[arg-type]
        selected=bool(run.selected),
        blocking_finding_count=run.blocking_finding_count or 0,
        review_finding_count=run.review_finding_count or 0,
        warning_finding_count=run.warning_finding_count or 0,
        deterministic_failure_count=run.deterministic_failure_count or 0,
        remediation_targets=list(run.remediation_targets or []),
        provider=run.first_pass_provider or "",
        model=run.first_pass_model or "",
        adjudicated=any(attempt.phase == "ADJUDICATION" for attempt in attempts),
        cost_microusd=run.cost_microusd or 0,
        report_asset_id=run.report_asset_id,
        contact_sheet_asset_id=run.contact_sheet_asset_id,
        error_code=run.error_code,
        row_version=_row_version(session, run.project_id),
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _check_projection(payload: dict[str, Any]) -> FinalCheckProjection:
    return FinalCheckProjection(
        check_id=UUID(str(payload["check_id"])),
        check_type=str(payload.get("check_type", "")),
        code=str(payload.get("code", "")),
        status=str(payload.get("status", "")),
        blocking=bool(payload.get("blocking", False)),
        measurement=payload.get("measurement"),
        threshold=payload.get("threshold"),
        unit=str(payload.get("unit", "")),
        start_us=payload.get("start_us"),
        end_us=payload.get("end_us"),
        cue_sequence=payload.get("cue_sequence"),
        tool=str(payload.get("tool", "")),
        tool_version=str(payload.get("tool_version", "")),
        message=str(payload.get("message", "")),
    )


def _finding_projection(
    payload: dict[str, Any], resolved: frozenset[UUID]
) -> FinalFindingProjection:
    finding_id = UUID(str(payload["finding_id"]))
    return FinalFindingProjection(
        finding_id=finding_id,
        category=str(payload.get("category", "")),
        severity=str(payload.get("severity", "")),
        blocking=bool(payload.get("blocking", False)),
        confidence=float(payload.get("confidence", 0.0)),
        issue_code=str(payload.get("issue_code", "")),
        summary=str(payload.get("summary", "")),
        start_us=int(payload.get("start_us", 0)),
        end_us=int(payload.get("end_us", 0)),
        shot_ids=[UUID(str(item)) for item in payload.get("shot_ids", [])],
        caption_cue_sequences=[int(item) for item in payload.get("caption_cue_sequences", [])],
        narration_segment_ids=[
            UUID(str(item)) for item in payload.get("narration_segment_ids", [])
        ],
        evidence=[
            FinalEvidenceProjection(
                evidence_id=UUID(str(item["evidence_id"])),
                evidence_type=str(item.get("evidence_type", "")),
                start_us=int(item.get("start_us", 0)),
                end_us=int(item.get("end_us", 0)),
                frame_asset_id=_optional_uuid(item.get("frame_asset_id")),
                sample_id=_optional_uuid(item.get("sample_id")),
                contact_sheet_asset_id=_optional_uuid(item.get("contact_sheet_asset_id")),
                contact_sheet_position=item.get("contact_sheet_position"),
                caption_cue_sequence=item.get("caption_cue_sequence"),
                shot_id=_optional_uuid(item.get("shot_id")),
                measurement=item.get("measurement"),
                threshold=item.get("threshold"),
                explanation=str(item.get("explanation", "")),
            )
            for item in payload.get("evidence", [])
        ],
        expected_behavior=str(payload.get("expected_behavior", "")),
        observed_behavior=str(payload.get("observed_behavior", "")),
        remediation_target=str(payload.get("remediation_target", "NONE")),
        provenance=str(payload.get("provenance", "deterministic")),
        resolved_by_review=finding_id in resolved,
    )


def _optional_uuid(value: Any) -> UUID | None:
    return None if value in (None, "") else UUID(str(value))


def _detail(
    session: SessionDep, blob: BlobDep, run: FinalEditorialRun
) -> FinalEditorialRunDetailProjection:
    report = report_payload(blob, session, run)
    resolved = FinalEditorialRepository(session).resolved_finding_ids(run.id)
    measurements = report.get("measurements")
    gate = report.get("gate", {})
    adjudication = report.get("adjudication") or {}
    inputs = report.get("inputs", {})
    base = _projection(session, run, report)
    return FinalEditorialRunDetailProjection(
        **base.model_dump(),
        measurements=(
            FinalMeasurementProjection(
                container_format=str(measurements.get("container_format", "")),
                byte_size=int(measurements.get("byte_size", 0)),
                video_codec=str(measurements.get("video_codec", "")),
                audio_codec=str(measurements.get("audio_codec", "")),
                width=measurements.get("width"),
                height=measurements.get("height"),
                pixel_format=str(measurements.get("pixel_format", "")),
                frame_rate=str(measurements.get("frame_rate", "")),
                container_duration_us=measurements.get("container_duration_us"),
                video_duration_us=measurements.get("video_duration_us"),
                audio_duration_us=measurements.get("audio_duration_us"),
                sample_rate_hz=measurements.get("sample_rate_hz"),
                channels=measurements.get("channels"),
                integrated_lufs=measurements.get("integrated_lufs"),
                true_peak_dbtp=measurements.get("true_peak_dbtp"),
                clipping_ratio=measurements.get("clipping_ratio"),
                video_decoded=bool(measurements.get("video_decoded", False)),
                audio_decoded=bool(measurements.get("audio_decoded", False)),
                black_interval_count=len(measurements.get("black_intervals", [])),
                freeze_interval_count=len(measurements.get("freeze_intervals", [])),
                silence_interval_count=len(measurements.get("silence_intervals", [])),
                ffmpeg_version=str(measurements.get("ffmpeg_version", "")),
                ffprobe_version=str(measurements.get("ffprobe_version", "")),
            )
            if isinstance(measurements, dict)
            else None
        ),
        media_checks=[_check_projection(item) for item in report.get("deterministic_checks", [])],
        audio_checks=[_check_projection(item) for item in report.get("audio_checks", [])],
        caption_checks=[_check_projection(item) for item in report.get("caption_checks", [])],
        dimensions=[
            FinalDimensionProjection(
                category=str(item.get("category", "")),
                applicable=bool(item.get("applicable", True)),
                score=float(item.get("score", 0.0)),
                confidence=float(item.get("confidence", 0.0)),
                blocking_finding_count=int(item.get("blocking_finding_count", 0)),
                review_finding_count=int(item.get("review_finding_count", 0)),
                warning_finding_count=int(item.get("warning_finding_count", 0)),
                summary=str(item.get("summary", "")),
            )
            for item in report.get("dimensions", [])
        ],
        findings=[_finding_projection(item, resolved) for item in report.get("findings", [])],
        remediation_routes=[
            FinalRemediationProjection(
                target=str(item.get("target", "NONE")),
                finding_ids=[UUID(str(value)) for value in item.get("finding_ids", [])],
                shot_ids=[UUID(str(value)) for value in item.get("shot_ids", [])],
                caption_cue_sequences=[
                    int(value) for value in item.get("caption_cue_sequences", [])
                ],
                reason=str(item.get("reason", "")),
                requires_new_render=bool(item.get("requires_new_render", True)),
            )
            for item in report.get("remediation_routes", [])
        ],
        adjudication_confidence=adjudication.get("confidence"),
        adjudication_decided=bool(adjudication.get("decided", False)),
        gate_reasons=[str(reason) for reason in gate.get("reasons", [])],
        timeline_duration_us=int(inputs.get("timeline_duration_us", 0)),
    )


# --- reads ---------------------------------------------------------------
@router.get("/{project_id}/final-qa", response_model=FinalEditorialCollectionResponse)
def list_final_editorial_runs(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, blob: BlobDep
) -> FinalEditorialCollectionResponse:
    project = owned_project(session, project_id, principal)
    repository = FinalEditorialRepository(session)
    items = [
        _projection(session, run, report_payload(blob, session, run))
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
        row_version=_row_version(session, project.id),
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
    body = _detail(session, blob, run)
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
