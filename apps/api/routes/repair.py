"""Owner-scoped T21 repair control plane.

Handlers stay thin and never call a provider, a renderer or a workflow: reads
are compact projections assembled from persisted rows, and an action records a
replayable decision that a worker or the CLI picks up. Cross-owner and
cross-project IDs return the same ``404`` as a missing one.

One rule is enforced here rather than only documented: no owner action can mark
a hard-failing visual as passed. Selection requires a new, valid T20 result, and
only a repair attempt can produce one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

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
from apps.api.schemas.repair import (
    RepairActionRequest,
    RepairActionResponse,
    RepairAttemptProjection,
    RepairBudgetProjection,
    RepairCollectionResponse,
    RepairDecisionProjection,
    RepairFallbackProjection,
    RepairPromptDeltaProjection,
    RepairRunDetailProjection,
    RepairRunProjection,
)
from vidgen.contracts.repair import RepairRunState
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.repair_models import (
    RepairAttemptRecord,
    RepairDecisionRecord,
    RepairFallbackRender,
    RepairRun,
)
from vidgen.db.repair_repository import RepairRepository
from vidgen.db.visual_qa_models import VisualQAResultRecord
from vidgen.review.errors import conflict, not_found
from vidgen.review.projections import resolve_shot

router = APIRouter(prefix="/projects", tags=["repair"])

ACTION_OPERATION = "repair:action"
# A repair is a property of one shot, so its mutations use the shot's row
# version rather than introducing a second concurrency token for the same shot.
REPAIR_RESOURCE = "shot"

#: Which actions a run in each state will accept. Anything else is reported as
#: stale rather than silently applied.
ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    RepairRunState.REPAIR_PLANNING.value: frozenset({"cancel", "retry"}),
    RepairRunState.REPAIRING.value: frozenset({"cancel", "retry"}),
    RepairRunState.ALTERNATE_PROVIDER.value: frozenset({"cancel", "retry"}),
    RepairRunState.FALLBACK_RENDERING.value: frozenset({"cancel", "retry"}),
    RepairRunState.REVALIDATING.value: frozenset({"cancel", "retry"}),
    RepairRunState.HUMAN_REVIEW_REQUIRED.value: frozenset(
        {"acknowledge", "resolve", "restart_after_reference_correction"}
    ),
    RepairRunState.REPAIR_FAILED.value: frozenset({"restart_after_reference_correction"}),
    RepairRunState.LOCKED.value: frozenset(),
}


def _require_run(session: SessionDep, project_id: UUID, shot_id: UUID, run_id: UUID) -> RepairRun:
    run = RepairRepository(session).run(project_id, run_id)
    if run is None or run.shot_id != shot_id:
        raise not_found("repair run")
    return run


def _delta(record: RepairAttemptRecord) -> RepairPromptDeltaProjection | None:
    stored = record.prompt_delta
    if not stored:
        return None
    return RepairPromptDeltaProjection(
        planner_version=str(stored.get("planner_version", "")),
        repair_reason=str(stored.get("repair_reason", "")),
        added_clauses=[str(item) for item in stored.get("added_clauses", [])],
        removed_clauses=[str(item) for item in stored.get("removed_clauses", [])],
        rewritten_clauses=[
            [str(item[0]), str(item[1])] for item in stored.get("rewritten_clauses", [])
        ],
        preserved_constraint_ids=[str(item) for item in stored.get("preserved_constraint_ids", [])],
        touched_constraint_ids=[str(item) for item in stored.get("touched_constraint_ids", [])],
        before_prompt_hash=str(stored.get("before_prompt_hash", "")),
        after_prompt_hash=str(stored.get("after_prompt_hash", "")),
        seed_changed=bool(stored.get("seed_changed", False)),
        previous_seed=stored.get("previous_seed"),
        new_seed=stored.get("new_seed"),
    )


def _attempt(session: SessionDep, record: RepairAttemptRecord) -> RepairAttemptProjection:
    qa = (
        session.get(VisualQAResultRecord, record.output_qa_result_id)
        if record.output_qa_result_id is not None
        else None
    )
    return RepairAttemptProjection(
        attempt_id=record.id,
        attempt_ordinal=record.attempt_ordinal,
        attempt_kind=record.attempt_kind,
        status=record.status,
        predecessor_attempt_id=record.predecessor_attempt_id,
        root_animation_attempt_id=record.root_animation_attempt_id,
        provider=record.provider,
        model=record.model,
        provider_operation_id=record.provider_operation_id,
        capability_profile_hash=record.capability_profile_hash,
        prompt_hash=record.prompt_hash,
        prompt_delta=_delta(record),
        seed=record.seed,
        output_asset_ids=[UUID(value) for value in record.output_asset_ids],
        output_qa_result_id=record.output_qa_result_id,
        qa_score=qa.recomputed_score if qa is not None else None,
        qa_outcome=qa.outcome if qa is not None else None,
        estimated_cost=record.estimated_cost,
        actual_cost=record.actual_cost,
        currency=record.currency,
        failure_category=record.failure_category,
        failure_code=record.failure_code,
        selected=bool(record.selected),
        created_at=record.created_at,
        completed_at=record.completed_at,
    )


def _decision(record: RepairDecisionRecord) -> RepairDecisionProjection:
    return RepairDecisionProjection(
        decision_id=record.id,
        sequence=record.sequence,
        route=record.route,
        rationale=[str(item) for item in (record.rationale or [])],
        failure_category=record.failure_category,
        repair_codes=[str(item) for item in (record.repair_codes or [])],
        human_review_reason=record.human_review_reason,
        estimated_next_cost=record.estimated_next_cost,
        budget_remaining=record.budget_remaining,
        planner_version=record.planner_version,
        policy_version=record.policy_version,
        created_at=record.created_at,
    )


def _projection(session: SessionDep, run: RepairRun) -> RepairRunProjection:
    classification = run.classification or {}
    triggering = session.get(VisualQAResultRecord, run.triggering_qa_result_id)
    return RepairRunProjection(
        repair_run_id=run.id,
        project_id=run.project_id,
        shot_id=run.shot_id,
        state=run.state,
        root_animation_attempt_id=run.root_animation_attempt_id,
        triggering_qa_result_id=run.triggering_qa_result_id,
        failure_category=classification.get("category"),
        failure_severity=classification.get("severity"),
        repair_code=classification.get("primary_code"),
        qa_score=triggering.recomputed_score if triggering is not None else None,
        pass_threshold=triggering.pass_threshold if triggering is not None else None,
        hard_failure=bool(triggering is not None and triggering.hard_failure),
        hard_failure_reason=(
            ", ".join(str(code) for code in (triggering.hard_failure_codes or []))
            if triggering is not None
            else None
        )
        or None,
        total_attempt_count=run.total_attempt_count,
        same_provider_repairs_used=run.same_provider_repairs_used,
        alternate_provider_attempts_used=run.alternate_provider_attempts_used,
        fallback_renders_used=run.fallback_renders_used,
        selected_attempt_id=run.selected_attempt_id,
        selected_asset_id=run.selected_asset_id,
        final_qa_result_id=run.final_qa_result_id,
        final_qa_score=float(run.final_qa_score) if run.final_qa_score is not None else None,
        human_review_reason=run.human_review_reason,
        human_review_resolved=run.human_review_resolved_at is not None,
        policy_version=run.policy_version,
        planner_version=run.planner_version,
        row_version=versions_for(session).current(run.project_id, REPAIR_RESOURCE, run.shot_id),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _budget(session: SessionDep, run: RepairRun) -> RepairBudgetProjection:
    repository = RepairRepository(session)
    attempts = repository.attempts(run.id)
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == run.project_id))
    policy = run.policy or {}
    limit = policy.get("per_shot_repair_cost_limit")
    return RepairBudgetProjection(
        currency=run.currency,
        total_repair_cost=run.total_repair_cost or Decimal("0"),
        estimated_repair_cost=sum((item.estimated_cost for item in attempts), Decimal("0")),
        per_shot_repair_cost_limit=Decimal(str(limit)) if limit is not None else None,
        project_hard_cap=budget.hard_cap if budget is not None else None,
        project_remaining=(
            budget.hard_cap - budget.committed_amount - budget.reserved_amount
            if budget is not None
            else None
        ),
    )


def _detail(session: SessionDep, run: RepairRun) -> RepairRunDetailProjection:
    repository = RepairRepository(session)
    attempts = repository.attempts(run.id)
    fallback = session.scalar(
        select(RepairFallbackRender).where(
            RepairFallbackRender.repair_attempt_id.in_([item.id for item in attempts] or [run.id])
        )
    )
    return RepairRunDetailProjection(
        **_projection(session, run).model_dump(),
        attempts=[_attempt(session, item) for item in attempts],
        decisions=[_decision(item) for item in repository.decisions(run.id)],
        fallback=(
            RepairFallbackProjection(
                repair_attempt_id=fallback.repair_attempt_id,
                renderer_version=fallback.renderer_version,
                render_identity=fallback.render_identity,
                input_asset_ids=[UUID(value) for value in fallback.input_asset_ids],
                exact_duration_us=fallback.exact_duration_us,
                width=fallback.width,
                height=fallback.height,
                frame_rate=fallback.frame_rate,
                pixel_format=fallback.pixel_format,
                video_codec=fallback.video_codec,
                output_asset_id=fallback.output_asset_id,
                manifest_asset_id=fallback.manifest_asset_id,
                qa_result_id=fallback.qa_result_id,
            )
            if fallback is not None
            else None
        ),
        budget=_budget(session, run),
    )


@router.get("/{project_id}/repairs", response_model=RepairCollectionResponse)
def list_project_repairs(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> RepairCollectionResponse:
    project = owned_project(session, project_id, principal)
    items = [
        _projection(session, run) for run in RepairRepository(session).runs_for_project(project.id)
    ]
    session.commit()
    return RepairCollectionResponse(project_id=project.id, items=items)


@router.get("/{project_id}/shots/{shot_id}/repairs", response_model=RepairCollectionResponse)
def list_shot_repairs(
    project_id: UUID, shot_id: UUID, session: SessionDep, principal: PrincipalDep
) -> RepairCollectionResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    items = [
        _projection(session, run)
        for run in RepairRepository(session).runs_for_shot(project.id, shot.id)
    ]
    session.commit()
    return RepairCollectionResponse(project_id=project.id, items=items)


@router.get(
    "/{project_id}/shots/{shot_id}/repairs/{repair_run_id}",
    response_model=RepairRunDetailProjection,
)
def get_repair_run(
    project_id: UUID,
    shot_id: UUID,
    repair_run_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
) -> RepairRunDetailProjection:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    run = _require_run(session, project.id, shot.id, repair_run_id)
    body = _detail(session, run)
    session.commit()
    set_etag(response, body.row_version)
    return body


@router.post(
    "/{project_id}/shots/{shot_id}/repairs/{repair_run_id}:act",
    response_model=RepairActionResponse,
)
def act_on_repair_run(
    project_id: UUID,
    shot_id: UUID,
    repair_run_id: UUID,
    request: RepairActionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> RepairActionResponse:
    project = owned_project(session, project_id, principal)
    shot = resolve_shot(session, project.id, shot_id)
    run = _require_run(session, project.id, shot.id, repair_run_id)
    idempotency = idempotency_for(session, principal)
    key = idempotency.require_key(ACTION_OPERATION, idempotency_key)
    payload = request.model_dump(mode="json")
    replay = idempotency.replay(ACTION_OPERATION, str(repair_run_id), key, payload)
    if replay is not None:
        return RepairActionResponse.model_validate(replay)
    versions = versions_for(session)
    expected = versions.require(project.id, REPAIR_RESOURCE, shot.id, if_match, label="shot")
    allowed = ALLOWED_ACTIONS.get(run.state, frozenset())
    if request.action not in allowed:
        raise conflict(
            ApiErrorCode.ATTEMPT_NOT_ELIGIBLE,
            f"a repair run in {run.state} does not accept {request.action}",
        )
    code = _apply(run, request)
    new_version = versions.bump(project.id, REPAIR_RESOURCE, shot.id, expected=expected)
    body = RepairActionResponse(
        repair_run_id=run.id,
        action=request.action,
        accepted=True,
        state=run.state,
        code=code,
        row_version=new_version,
    )
    idempotency.record(
        ACTION_OPERATION,
        str(repair_run_id),
        key,
        payload,
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, new_version)
    return body


def _apply(run: RepairRun, request: RepairActionRequest) -> str:
    """Record the owner's decision. Nothing here selects or passes an output."""
    if request.action == "cancel":
        # Honoured between paid attempts: the worker checks this flag before it
        # routes, so an in-flight provider call is never orphaned mid-charge.
        run.cancellation_requested = True
        return "cancellation_requested"
    if request.action == "retry":
        # Resuming a durable technical operation, not a new paid generation.
        run.cancellation_requested = False
        return "resume_requested"
    if request.action in {"acknowledge", "resolve"}:
        run.human_review_resolved_at = datetime.now(UTC)
        return f"human_review_{request.action}d"
    # An upstream T19 reference was corrected, so the shot may be repaired
    # again from a valid reference. The previous attempts stay as history.
    run.state = RepairRunState.REPAIR_PLANNING.value
    run.human_review_reason = None
    run.human_review_resolved_at = datetime.now(UTC)
    run.cancellation_requested = False
    return "restarted_after_upstream_correction"
