"""Cost and telemetry instrumentation for the Episode Analyst port.

Episode analysis fans out one provider call per scene and then issues a global
reduce call, and every one of those is billable. This wrapper routes each call
through the same T23 infrastructure the other paid stages use: a durable
provider attempt, a priced estimate, a transactional budget reservation, usage
recorded from the provider's own token counts, then reconciliation into the
project cost ledger. Without it the analysis stage spends real money that never
reaches the ledger the project dashboard reads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.analysis.provider import EpisodeAnalysisProvider, GenerationContext
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.episode_analysis import (
    EpisodeSynthesisRequest,
    ProviderEpisodeAnalysisResult,
    ProviderMetadata,
    ProviderSceneAnalysisResult,
    SceneAnalysisRequest,
)
from vidgen.costs import openai_rates
from vidgen.db.cost_models import CostReservation, ProjectBudget, ProviderPriceRate
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import ProviderAttemptContext, instrument_provider_attempt

SCENE_OPERATION = "episode_analysis.scene"
REDUCE_OPERATION = "episode_analysis.reduce"

#: Characters per token, used only to price a call *before* it is made. The
#: reconciled amount always comes from the token counts the provider reports.
ESTIMATED_CHARACTERS_PER_TOKEN = 4
#: Rough output envelope per operation, again estimate-only.
ESTIMATED_OUTPUT_TOKENS = {SCENE_OPERATION: 1_500, REDUCE_OPERATION: 6_000}


class PricedAmount(NamedTuple):
    """One priced quantity, and where its price came from."""

    amount: Decimal
    pricing_version_id: UUID | None
    #: "catalog" when every unit was priced from ``provider_price_rates``,
    #: "fallback" when any unit fell back to the published list prices in
    #: :mod:`vidgen.costs.openai_rates`.
    source: str


AnalysisRequest = SceneAnalysisRequest | EpisodeSynthesisRequest
AnalysisResult = ProviderSceneAnalysisResult | ProviderEpisodeAnalysisResult


class InstrumentedEpisodeAnalysisProvider:
    """Wraps an :class:`EpisodeAnalysisProvider` with attempt and cost recording."""

    def __init__(
        self,
        session: Session,
        provider: EpisodeAnalysisProvider,
        *,
        tracer: Tracer | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.session = session
        self.inner = provider
        self.tracer = tracer or trace.NoOpTracerProvider().get_tracer("vidgen.episode_analysis")
        self.metrics = metrics or Metrics()
        self.costs = CostRepository(session)

    @property
    def provider(self) -> str:
        return str(getattr(self.inner, "provider", type(self.inner).__name__))

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "configured"))

    @property
    def configuration_version(self) -> str:
        return str(getattr(self.inner, "configuration_version", "episode-provider-v1"))

    async def analyze_scene(
        self, request: SceneAnalysisRequest, context: GenerationContext
    ) -> ProviderSceneAnalysisResult:
        result = await self._call(
            SCENE_OPERATION,
            request,
            context,
            lambda: self.inner.analyze_scene(request, context),
        )
        assert isinstance(result, ProviderSceneAnalysisResult)
        return result

    async def synthesize_episode(
        self, request: EpisodeSynthesisRequest, context: GenerationContext
    ) -> ProviderEpisodeAnalysisResult:
        result = await self._call(
            REDUCE_OPERATION,
            request,
            context,
            lambda: self.inner.synthesize_episode(request, context),
        )
        assert isinstance(result, ProviderEpisodeAnalysisResult)
        return result

    def _rate(self, operation: str, usage_unit: str) -> ProviderPriceRate | None:
        return self.session.scalar(
            select(ProviderPriceRate)
            .where(
                ProviderPriceRate.provider == self.provider,
                ProviderPriceRate.model == self.model,
                ProviderPriceRate.operation == operation,
                ProviderPriceRate.usage_unit == usage_unit,
                ProviderPriceRate.active,
            )
            .order_by(ProviderPriceRate.effective_start.desc())
        )

    def _price(self, operation: str, quantities: dict[str, Decimal | None]) -> PricedAmount:
        """Price token quantities from the catalog, then from published list prices."""
        total = Decimal("0")
        pricing_version_id: UUID | None = None
        source = "catalog"
        for unit, quantity in quantities.items():
            if quantity is None:
                continue
            rate = self._rate(operation, unit)
            if rate is not None:
                pricing_version_id = rate.pricing_version_id
                total += quantity / rate.unit_size * rate.unit_price
                continue
            published = openai_rates.unit_price(self.model, unit)
            if published is None:
                # No catalog rate and no published price for this model: record
                # the tokens and leave the money at zero rather than invent it.
                source = "unpriced"
                continue
            total += quantity * published
            if source != "unpriced":
                source = "fallback"
        return PricedAmount(total, pricing_version_id, source)

    def _estimate(self, operation: str, request: AnalysisRequest) -> PricedAmount:
        input_tokens = Decimal(
            max(1, len(request.model_dump_json()) // ESTIMATED_CHARACTERS_PER_TOKEN)
        )
        return self._price(
            operation,
            {
                "CACHED_INPUT_TOKEN": None,
                "INPUT_TOKEN": input_tokens,
                "OUTPUT_TOKEN": Decimal(ESTIMATED_OUTPUT_TOKENS[operation]),
            },
        )

    def _reserve(
        self, *, project_id: UUID, provider_attempt_id: UUID, identity: str, estimated: Decimal
    ) -> UUID | None:
        budget = self.session.scalar(
            select(ProjectBudget.id).where(ProjectBudget.project_id == project_id)
        )
        if budget is None:
            return None
        key = f"{identity}:reservation"
        settled = self.session.scalar(
            select(CostReservation).where(CostReservation.idempotency_key == key)
        )
        if settled is not None and settled.status != "RESERVED":
            # A resumed run replayed a call whose reservation was already
            # committed or released. Its ledger entry stands; reconciling it a
            # second time would only fail.
            return None
        reservation = self.costs.reserve(
            CostReservationRequest(
                project_id=project_id,
                provider_attempt_id=provider_attempt_id,
                idempotency_key=key,
                estimated_amount=estimated,
                currency="USD",
            )
        )
        if reservation.decision in {
            BudgetDecision.DENY_HARD_CAP,
            BudgetDecision.DENY_ENTITY_CAP,
            BudgetDecision.UNKNOWN_PRICE_REVIEW,
        }:
            raise BudgetExceededError(f"episode analysis denied: {reservation.decision}")
        return reservation.reservation_id

    async def _call(
        self,
        operation: str,
        request: AnalysisRequest,
        context: GenerationContext,
        invoke: Callable[[], Awaitable[AnalysisResult]],
    ) -> AnalysisResult:
        estimate = self._estimate(operation, request)
        # One physical provider call, one identity. A retry after a provider
        # error reuses the request's key, so the attempt number is what keeps
        # its attempt row, reservation and ledger entry distinct from the call
        # that already failed and released its budget.
        identity = f"{request.idempotency_key}:attempt:{context.attempt_number}"
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=request.project_id,
            provider=self.provider,
            model=self.model,
            operation=operation,
            input_hash=request.input_hash,
            idempotency_key=identity,
            related_entity_id=getattr(request, "scene_id", None),
            attempt_number=context.attempt_number,
            estimated_cost=estimate.amount,
            pricing_version_id=estimate.pricing_version_id,
        ) as attempt:
            reservation_id = self._reserve(
                project_id=request.project_id,
                provider_attempt_id=attempt.row.id,
                identity=identity,
                estimated=estimate.amount,
            )
            try:
                result = await invoke()
            except BaseException:
                # Release the reservation before unwinding, otherwise a failed
                # call would hold budget against the project forever.
                if reservation_id is not None:
                    self.costs.reconcile(
                        reservation_id,
                        f"{identity}:reconciliation",
                        Decimal("0"),
                        billable=False,
                    )
                raise
            actual = self._record(attempt, operation, result.metadata)
            if reservation_id is not None:
                self.costs.reconcile(reservation_id, f"{identity}:reconciliation", actual)
        return result

    def _record(
        self, attempt: ProviderAttemptContext, operation: str, metadata: ProviderMetadata
    ) -> Decimal:
        quantities = _token_quantities(metadata)
        priced = self._price(operation, quantities)
        for unit, quantity in quantities.items():
            direction = "output" if unit == "OUTPUT_TOKEN" else "input"
            if quantity is not None:
                self.metrics.tokens.labels(self.model, direction, "episode_analyst").inc(
                    float(quantity)
                )
        attempt.set_result(
            provider_request_id=metadata.provider_request_id,
            usage=[
                {"unit": unit, "quantity": int(quantity)}
                for unit, quantity in quantities.items()
                if quantity is not None
            ],
            metadata={
                **metadata.redacted_response_metadata,
                # A call priced off the published list rather than a catalog
                # rate says so, so an amount that came from a default is never
                # mistaken for one the pricing catalog stands behind.
                "pricing_status": priced.source,
            },
            actual_cost=priced.amount,
        )
        # ``instrument_provider_attempt`` copies usage onto the row only when
        # the block exits cleanly, and the ledger entry is written before that,
        # from the row. Assign it now so the entry carries the usage it prices.
        attempt.row.usage = attempt.usage
        return priced.amount


def _token_quantities(metadata: ProviderMetadata) -> dict[str, Decimal | None]:
    """Split the provider's reported usage into the units that are billed apart.

    ``input_tokens`` is the total, cache hits included, and the cached share is
    billed at a lower rate. Recording the uncached remainder under
    ``INPUT_TOKEN`` keeps the three units disjoint, so summing a ledger entry's
    usage returns the tokens the call actually spent.
    """
    cached = metadata.cached_input_tokens
    total_input = metadata.input_tokens
    uncached = None if total_input is None else Decimal(total_input - (cached or 0))
    return {
        "CACHED_INPUT_TOKEN": None if not cached else Decimal(cached),
        "INPUT_TOKEN": uncached,
        "OUTPUT_TOKEN": None if metadata.output_tokens is None else Decimal(metadata.output_tokens),
    }
