"""Versioned Runway pricing, derived from the centralized capability registry.

The per-second credit price of each model is declared once, on its
``VideoCapability``; this module only projects it into the T23 catalog shape and
the per-request estimate. Verified against the official pricing guide on the
registry's verification date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from services.animation.providers import (
    CAPABILITIES,
    CAPABILITY_VERIFICATION_DATE,
    CREDIT_USD,
    capability_for,
)
from vidgen.contracts.costs import PricingCatalogVersion, PricingRate
from vidgen.contracts.telemetry import UsageUnit

PRICING_CONFIGURATION_VERSION = "runway-pricing-2026-09-09"
PRICING_SOURCE = "https://docs.dev.runwayml.com/guides/pricing/"
PRICING_VERIFICATION_DATE = CAPABILITY_VERIFICATION_DATE
CREDITS_PER_SECOND: dict[str, Decimal] = {
    model: Decimal(capability.credits_per_second) for model, capability in CAPABILITIES.items()
}
MONEY = Decimal("0.000001")


def runway_pricing_catalog() -> PricingCatalogVersion:
    """Return the immutable T23 catalog projection used for T15 estimates."""
    version_id = uuid5(NAMESPACE_URL, PRICING_CONFIGURATION_VERSION)
    effective = datetime(
        PRICING_VERIFICATION_DATE.year,
        PRICING_VERIFICATION_DATE.month,
        PRICING_VERIFICATION_DATE.day,
        tzinfo=UTC,
    )
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


def unit_price(model: str) -> Decimal:
    """USD per generated second for one model."""
    return capability_for(model).unit_price_usd


def estimate_runway_cost(model: str, duration_seconds: float) -> Decimal:
    """Price one generation of ``duration_seconds`` on ``model``.

    Runway bills whole generated seconds, so a fractional request is priced at
    the duration the registry would actually submit for it.
    """
    try:
        capability = capability_for(model)
    except ValueError as error:
        raise ValueError(f"unknown Runway pricing model: {model}") from error
    billed = capability.select_duration(duration_seconds)
    seconds = Decimal(billed) if billed is not None else Decimal(str(duration_seconds))
    return (capability.unit_price_usd * seconds).quantize(MONEY)
