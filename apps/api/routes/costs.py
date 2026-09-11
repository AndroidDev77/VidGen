from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_session
from apps.api.routes.projects import owned_project
from vidgen.db.animation_models import RunwayTask
from vidgen.db.cost_models import (
    CostLedgerEntry,
    PipelineFailureEvent,
    ProjectBudget,
    ProviderAttempt,
)
from vidgen.review.projections import utc

router = APIRouter(prefix="/projects", tags=["costs", "operations"])
S = Annotated[Session, Depends(get_session)]
P = Annotated[Principal, Depends(get_current_user)]


@router.get("/{project_id}/costs")
def costs(project_id: UUID, session: S, principal: P) -> dict[str, object]:
    owned_project(session, project_id, principal)
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project_id))
    rows = session.scalars(
        select(CostLedgerEntry).where(CostLedgerEntry.project_id == project_id)
    ).all()

    def breakdown(field: str) -> dict[str, str]:
        out: dict[str, Decimal] = {}
        for row in rows:
            out[str(getattr(row, field))] = (
                out.get(str(getattr(row, field)), Decimal(0)) + row.actual_amount
            )
        return {k: str(v) for k, v in sorted(out.items())}

    # The routing reason lives on the provider attempt each ledger entry settles,
    # so spend can be read by *why* a model was chosen, not only by which model.
    attempt_ids = {row.provider_attempt_id for row in rows}
    attempts = (
        {
            attempt.id: attempt
            for attempt in session.scalars(
                select(ProviderAttempt).where(ProviderAttempt.id.in_(attempt_ids))
            )
        }
        if attempt_ids
        else {}
    )
    by_routing_reason: dict[str, Decimal] = {}
    by_quality_mode: dict[str, Decimal] = {}
    for row in rows:
        attempt = attempts.get(row.provider_attempt_id)
        metadata = attempt.redacted_metadata if attempt is not None else {}
        reason = str(metadata.get("routing_reason") or "unrouted")
        mode = str(metadata.get("quality_mode") or "unrouted")
        by_routing_reason[reason] = by_routing_reason.get(reason, Decimal(0)) + row.actual_amount
        by_quality_mode[mode] = by_quality_mode.get(mode, Decimal(0)) + row.actual_amount
    committed = sum((r.actual_amount for r in rows), Decimal(0))
    reserved = budget.reserved_amount if budget else Decimal(0)
    released = sum((r.released_amount for r in rows), Decimal(0))
    hard = budget.hard_cap if budget else Decimal(0)
    warning = budget.warning_cap if budget else Decimal(0)
    return {
        "projectId": str(project_id),
        "warningCap": str(warning),
        "hardCap": str(hard),
        "reservedAmount": str(reserved),
        "committedAmount": str(committed),
        "releasedAmount": str(released),
        "remainingAmount": str(hard - committed - reserved),
        "warningPercentage": str(committed / warning * 100) if warning else None,
        "hardPercentage": str(committed / hard * 100) if hard else None,
        "byProvider": breakdown("provider"),
        "byModel": breakdown("model"),
        "byOperation": breakdown("operation"),
        "byReason": breakdown("reason"),
        "byRoutingReason": {k: str(v) for k, v in sorted(by_routing_reason.items())},
        "byQualityMode": {k: str(v) for k, v in sorted(by_quality_mode.items())},
    }


@router.get("/{project_id}/provider-attempts")
def attempts(
    project_id: UUID,
    session: S,
    principal: P,
    provider: str | None = None,
    model: str | None = None,
    operation: str | None = None,
    status: str | None = None,
    failure_class: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    owned_project(session, project_id, principal)
    query = (
        select(ProviderAttempt, RunwayTask.failure_message)
        .outerjoin(RunwayTask, RunwayTask.provider_attempt_id == ProviderAttempt.id)
        .where(ProviderAttempt.project_id == project_id)
    )
    for column, value in (
        (ProviderAttempt.provider, provider),
        (ProviderAttempt.model, model),
        (ProviderAttempt.operation, operation),
        (ProviderAttempt.status, status),
        (ProviderAttempt.failure_class, failure_class),
    ):
        if value is not None:
            query = query.where(column == value)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(
        query.order_by(ProviderAttempt.started_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(r.ProviderAttempt.id),
                "provider": r.ProviderAttempt.provider,
                "model": r.ProviderAttempt.model,
                "operation": r.ProviderAttempt.operation,
                "status": r.ProviderAttempt.status,
                "failureClass": r.ProviderAttempt.failure_class,
                "latencyMs": r.ProviderAttempt.latency_ms,
                "startedAt": r.ProviderAttempt.started_at,
                "errorMessage": r.failure_message,
            }
            for r in rows
        ],
    }


def _iso(value: datetime | None) -> str | None:
    """A timestamp the browser can parse as UTC, or nothing at all.

    ``utc`` stamps the zone onto the naive values SQLite hands back; without it
    the browser reads a UTC instant as local time and the dashboard reports a
    failure hours away from when it happened.
    """
    stamped = utc(value)
    return stamped.isoformat() if stamped is not None else None


@router.get("/{project_id}/failures")
def failures(
    project_id: UUID,
    session: S,
    principal: P,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    owned_project(session, project_id, principal)
    rows = session.scalars(
        select(PipelineFailureEvent)
        .where(PipelineFailureEvent.project_id == project_id)
        .order_by(PipelineFailureEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "workflowId": r.workflow_id,
                "stage": r.stage,
                "failureClass": r.failure_class,
                "errorCode": r.error_code,
                "retryable": r.retryable,
                "status": r.projected_status,
                # The dashboard needs both to tell "this is why the project is
                # stuck" from "this is something that already recovered".
                "createdAt": _iso(r.created_at),
                "resolvedAt": _iso(r.resolved_at),
            }
            for r in rows
        ]
    }
