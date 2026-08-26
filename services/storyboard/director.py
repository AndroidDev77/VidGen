"""Instrumented Storyboard Director invocation.

Every production director call goes through the existing T23 infrastructure:
durable provider-attempt identity, pricing-catalog estimate, transactional budget
reservation, trace propagation, a pre-call checkpoint, then usage recording,
reconciliation, bounded metrics, and failure classification. No competing
telemetry or cost infrastructure is created here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.storyboard.canonicalize import canonicalize_provider_result
from services.storyboard.providers import StoryboardDirector
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.storyboard import StoryboardProviderRequest, StoryboardProviderResult
from vidgen.db.cost_models import ProjectBudget, ProviderPriceRate
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

STORYBOARD_OPERATION = "storyboard.direct"
#: Rough envelope size used only to price the call before it is made.
ESTIMATED_OUTPUT_TOKENS_PER_SHOT = 320


@dataclass(frozen=True, slots=True)
class DirectorCallOutcome:
    result: StoryboardProviderResult
    provider_attempt_id: UUID
    estimated_cost: Decimal
    actual_cost: Decimal


class InstrumentedStoryboardDirector:
    def __init__(
        self,
        session: Session,
        provider: StoryboardDirector,
        *,
        tracer: Tracer,
        metrics: Metrics,
    ) -> None:
        self.session = session
        self.provider = provider
        self.tracer = tracer
        self.metrics = metrics
        self.costs = CostRepository(session)

    def _rate(self, usage_unit: str) -> ProviderPriceRate | None:
        return self.session.scalar(
            select(ProviderPriceRate)
            .where(
                ProviderPriceRate.provider == self.provider.name,
                ProviderPriceRate.model == self.provider.model,
                ProviderPriceRate.operation == STORYBOARD_OPERATION,
                ProviderPriceRate.usage_unit == usage_unit,
                ProviderPriceRate.active,
            )
            .order_by(ProviderPriceRate.effective_start.desc())
        )

    def estimate_cost(self, request: StoryboardProviderRequest, expected_shots: int) -> Decimal:
        quantities = {
            "INPUT_TOKEN": Decimal(len(request.narration_text.split()) * 2 + 512),
            "OUTPUT_TOKEN": Decimal(max(1, expected_shots) * ESTIMATED_OUTPUT_TOKENS_PER_SHOT),
        }
        total = Decimal("0")
        for unit, quantity in quantities.items():
            rate = self._rate(unit)
            if rate is not None:
                total += quantity / rate.unit_size * rate.unit_price
        return total

    def _actual_cost(self, result: StoryboardProviderResult) -> Decimal:
        total = Decimal("0")
        for unit, quantity in (
            ("INPUT_TOKEN", result.usage.get("input_tokens")),
            ("OUTPUT_TOKEN", result.usage.get("output_tokens")),
        ):
            if quantity is None:
                continue
            rate = self._rate(unit)
            if rate is not None:
                total += Decimal(str(quantity)) / rate.unit_size * rate.unit_price
        return total

    async def direct(
        self,
        request: StoryboardProviderRequest,
        *,
        input_hash: str,
        related_entity_id: UUID,
        expected_shots: int,
    ) -> DirectorCallOutcome:
        estimated = self.estimate_cost(request, expected_shots)
        pricing_rate = self._rate("OUTPUT_TOKEN") or self._rate("INPUT_TOKEN")
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=request.project_id,
            provider=self.provider.name,
            model=self.provider.model,
            operation=STORYBOARD_OPERATION,
            input_hash=input_hash,
            idempotency_key=request.idempotency_key,
            related_entity_id=related_entity_id,
            attempt_number=request.attempt_number,
            estimated_cost=estimated,
            pricing_version_id=pricing_rate.pricing_version_id if pricing_rate else None,
        ) as attempt:
            reservation = None
            has_budget = self.session.scalar(
                select(ProjectBudget).where(ProjectBudget.project_id == request.project_id)
            )
            if has_budget is not None:
                reservation = self.costs.reserve(
                    CostReservationRequest(
                        project_id=request.project_id,
                        provider_attempt_id=attempt.row.id,
                        idempotency_key=f"{request.idempotency_key}:reservation",
                        estimated_amount=estimated,
                        currency="USD",
                    )
                )
                if reservation.decision in (
                    BudgetDecision.DENY_HARD_CAP,
                    BudgetDecision.DENY_ENTITY_CAP,
                ):
                    raise BudgetExceededError(
                        "storyboard direction request denied by the project budget"
                    )
            raw = await self.provider.propose(request)
            result = canonicalize_provider_result(raw)
            actual = self._actual_cost(result)
            attempt.set_result(
                provider_request_id=result.provider_request_id,
                usage=[
                    {"unit": unit, "quantity": quantity}
                    for unit, quantity in sorted(result.usage.items())
                ],
                metadata=dict(result.redacted_response_metadata),
                actual_cost=actual,
            )
            if reservation is not None and reservation.reservation_id is not None:
                self.costs.reconcile(
                    reservation.reservation_id,
                    f"{request.idempotency_key}:reconciliation",
                    actual,
                )
        return DirectorCallOutcome(
            result=result,
            provider_attempt_id=attempt.row.id,
            estimated_cost=estimated,
            actual_cost=actual,
        )
