from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_session
from apps.api.routes.projects import owned_project
from vidgen.db.cost_models import (
    CostLedgerEntry,
    PipelineFailureEvent,
    ProjectBudget,
    ProviderAttempt,
)

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
    query = select(ProviderAttempt).where(ProviderAttempt.project_id == project_id)
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
    rows = session.scalars(
        query.order_by(ProviderAttempt.started_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(r.id),
                "provider": r.provider,
                "model": r.model,
                "operation": r.operation,
                "status": r.status,
                "failureClass": r.failure_class,
                "latencyMs": r.latency_ms,
                "startedAt": r.started_at,
            }
            for r in rows
        ],
    }


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
            }
            for r in rows
        ]
    }
