from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from vidgen.contracts.costs import CostEstimate, CostLineItem
from vidgen.contracts.telemetry import UsageQuantity
from vidgen.costs.pricing import PricingCatalog

MONEY = Decimal("0.000001")


def calculate_cost(
    catalog: PricingCatalog,
    provider: str,
    model: str,
    operation: str,
    usage: tuple[UsageQuantity, ...],
    at: datetime,
    *,
    minimum_charge: Decimal = Decimal("0"),
    unknown_policy: str = "warn",
) -> CostEstimate:
    lines = []
    warnings = []
    for item in usage:
        rate = catalog.select(provider, model, operation, item.unit, at)
        if rate is None:
            warnings.append(f"UNKNOWN_PRICE:{provider}:{model}:{operation}:{item.unit}")
            if unknown_policy == "deny":
                raise ValueError("unknown provider price")
            continue
        amount = (item.quantity / rate.unit_size * rate.unit_price).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        lines.append(CostLineItem(usage=item, rate=rate, amount=amount))
    total = max(minimum_charge, sum((line.amount for line in lines), Decimal("0"))).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )
    return CostEstimate(
        line_items=tuple(lines),
        estimated_cost=total,
        actual_cost=None,
        currency=catalog.version.currency,
        pricing_version_id=catalog.version.id,
        warnings=tuple(warnings),
    )
