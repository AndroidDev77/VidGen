from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from vidgen.contracts.costs import BudgetDecision, BudgetPolicy, PricingCatalogVersion, PricingRate
from vidgen.contracts.telemetry import FailureClass, UsageQuantity, UsageUnit
from vidgen.costs.budgets import decide_budget
from vidgen.costs.calculator import calculate_cost
from vidgen.costs.pricing import PricingCatalog
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.redaction import REDACTED, redact

VERSION = UUID("00000000-0000-0000-0000-000000000023")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def rate() -> PricingRate:
    return PricingRate(
        pricing_version_id=VERSION,
        provider="fake",
        model="fake-1",
        operation="analyze",
        usage_unit=UsageUnit.INPUT_TOKEN,
        unit_size=Decimal("1000"),
        unit_price=Decimal("0.25"),
        currency="USD",
        effective_start=NOW,
        source_reference="fixture://pricing",
        verification_date=date(2026, 1, 1),
    )


def test_recursive_redaction_never_exposes_secrets() -> None:
    value = redact(
        {
            "Authorization": "Bearer secret",
            "nested": {"prompt": "private"},
            "url": "https://blob.test/a?sig=secret&x=ok",
            "bytes": b"secret",
        }
    )
    rendered = str(value)
    assert "secret" not in rendered
    assert value["Authorization"] == REDACTED
    assert "x=ok" in rendered


def test_decimal_pricing_and_budget_decisions_are_deterministic() -> None:
    catalog = PricingCatalog(
        PricingCatalogVersion(id=VERSION, name="fixture", currency="USD", rates=(rate(),))
    )
    result = calculate_cost(
        catalog,
        "fake",
        "fake-1",
        "analyze",
        (UsageQuantity(unit=UsageUnit.INPUT_TOKEN, quantity=Decimal("1500")),),
        NOW,
    )
    assert result.estimated_cost == Decimal("0.375000")
    policy = BudgetPolicy(
        version="1", warning_cap=Decimal("1"), hard_cap=Decimal("2"), currency="USD"
    )
    assert (
        decide_budget(policy, Decimal("0.9"), Decimal("0"), Decimal("0.2")).decision
        == BudgetDecision.ALLOW_WITH_WARNING
    )
    assert (
        decide_budget(policy, Decimal("1.9"), Decimal("0"), Decimal("0.2")).decision
        == BudgetDecision.DENY_HARD_CAP
    )


def test_overlapping_rates_are_rejected() -> None:
    with pytest.raises(ValueError, match="overlapping"):
        PricingCatalog(
            PricingCatalogVersion(id=VERSION, name="bad", currency="USD", rates=(rate(), rate()))
        )


def test_failure_taxonomy_and_bounded_metrics() -> None:
    failure = classify_failure(TimeoutError("raw secret"))
    assert failure.failure_class == FailureClass.TIMEOUT
    assert failure.retryable
    assert "raw secret" not in failure.sanitized_message
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    metrics.provider_requests.labels("fake", "fake-1", "analyze", "success").inc()
    output = generate_latest(registry).decode()
    assert "recap_provider_requests_total" in output
    assert "project_id" not in output
