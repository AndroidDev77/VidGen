"""Versioned Runway pricing verified against official documentation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from vidgen.contracts.costs import PricingCatalogVersion, PricingRate
from vidgen.contracts.telemetry import UsageUnit

PRICING_CONFIGURATION_VERSION = "runway-pricing-2026-08-27"
PRICING_SOURCE = "https://docs.dev.runwayml.com/guides/pricing/"
PRICING_VERIFICATION_DATE = date(2026, 8, 27)
CREDIT_USD = Decimal("0.01")
CREDITS_PER_SECOND = {
    "gen4_turbo": Decimal("5"),
    "gen4.5": Decimal("12"),
}


def runway_pricing_catalog() -> PricingCatalogVersion:
    """Return the immutable T23 catalog projection used for T15 estimates."""
    version_id = uuid5(NAMESPACE_URL, PRICING_CONFIGURATION_VERSION)
    effective = datetime(2026, 8, 27, tzinfo=UTC)
    rates = tuple(
        PricingRate(
            pricing_version_id=version_id,
            provider="runway",
            model=model,
            operation="video_generation",
            usage_unit=UsageUnit.VIDEO_OUTPUT_SECOND,
            unit_size=Decimal("1"),
            unit_price=credits * CREDIT_USD,
            currency="USD",
            effective_start=effective,
            source_reference=PRICING_SOURCE,
            verification_date=PRICING_VERIFICATION_DATE,
            notes=(
                f"{credits} credits/generated second; $0.01/credit; "
                f"configuration={PRICING_CONFIGURATION_VERSION}"
            ),
        )
        for model, credits in CREDITS_PER_SECOND.items()
    )
    return PricingCatalogVersion(
        id=version_id,
        name=PRICING_CONFIGURATION_VERSION,
        currency="USD",
        rates=rates,
    )


def estimate_runway_cost(model: str, duration_seconds: float) -> Decimal:
    try:
        credits = CREDITS_PER_SECOND[model] * Decimal(str(duration_seconds))
    except KeyError as error:
        raise ValueError(f"unknown Runway pricing model: {model}") from error
    return (credits * CREDIT_USD).quantize(Decimal("0.000001"))
