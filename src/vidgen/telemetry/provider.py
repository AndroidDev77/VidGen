from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.cost_models import ProviderAttempt
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.redaction import redact


class ProviderAttemptContext:
    def __init__(self, row: ProviderAttempt):
        self.row = row
        self.usage: list[dict[str, object]] = []
        self.actual_cost = Decimal("0")

    def set_result(
        self,
        *,
        provider_request_id: str | None = None,
        usage: list[dict[str, object]] | None = None,
        actual_cost: Decimal = Decimal("0"),
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.row.provider_request_id = provider_request_id
        self.usage = usage or []
        self.actual_cost = actual_cost
        self.row.redacted_metadata = redact(metadata or {})


@asynccontextmanager
async def instrument_provider_attempt(
    *,
    session: Session,
    tracer: Tracer,
    metrics: Metrics,
    project_id: UUID,
    provider: str,
    model: str,
    operation: str,
    input_hash: str,
    idempotency_key: str,
    related_entity_id: UUID | None = None,
    attempt_number: int = 1,
    estimated_cost: Decimal = Decimal("0"),
    pricing_version_id: UUID | None = None,
) -> AsyncIterator[ProviderAttemptContext]:
    row = session.scalar(
        select(ProviderAttempt).where(
            ProviderAttempt.project_id == project_id,
            ProviderAttempt.provider == provider,
            ProviderAttempt.operation == operation,
            ProviderAttempt.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        row = ProviderAttempt(
            id=uuid4(),
            project_id=project_id,
            related_entity_type=None,
            related_entity_id=related_entity_id,
            operation=operation,
            attempt_number=attempt_number,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            provider=provider,
            model=model,
            provider_configuration_version="1",
            status="STARTED",
            usage=[],
            estimated_cost=estimated_cost,
            actual_cost=0,
            pricing_version_id=pricing_version_id,
            currency="USD",
            started_at=datetime.now(UTC),
            redacted_metadata={},
        )
        session.add(row)
        session.flush()
    started = time.monotonic()
    labels = (provider, model, operation)
    metrics.provider_active.labels(provider, model).inc()
    with tracer.start_as_current_span("provider.request") as span:
        span.set_attribute("provider.name", provider)
        span.set_attribute("provider.model", model)
        span.set_attribute("provider.operation", operation)
        span.set_attribute("provider.attempt_id", str(row.id))
        ctx = ProviderAttemptContext(row)
        try:
            yield ctx
            row.status = "SUCCEEDED"
            row.usage = ctx.usage
            row.actual_cost = ctx.actual_cost
            if row.provider_request_id:
                span.set_attribute("provider.request_id", row.provider_request_id)
            metrics.provider_requests.labels(*labels, "success").inc()
            metrics.cost.labels(*labels).inc(float(ctx.actual_cost))
        except BaseException as exc:
            failure = classify_failure(exc)
            row.status = "FAILED"
            row.failure_class = failure.failure_class
            row.error_code = failure.error_code
            row.retryable = failure.retryable
            status = "cancelled" if failure.failure_class == "CANCELLED" else "failure"
            metrics.provider_requests.labels(*labels, status).inc()
            if failure.failure_class == "RATE_LIMIT":
                metrics.rate_limits.labels(provider, model).inc()
            raise
        finally:
            elapsed = time.monotonic() - started
            row.latency_ms = int(elapsed * 1000)
            row.completed_at = datetime.now(UTC)
            metrics.provider_latency.labels(*labels).observe(elapsed)
            metrics.provider_active.labels(provider, model).dec()
            session.flush()
