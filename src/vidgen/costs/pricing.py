from __future__ import annotations

from datetime import datetime

from vidgen.contracts.costs import PricingCatalogVersion, PricingRate
from vidgen.contracts.telemetry import UsageUnit


class PricingCatalog:
    def __init__(self, version: PricingCatalogVersion):
        self.version = version
        self._validate()

    def _validate(self) -> None:
        rates = list(self.version.rates)
        for i, left in enumerate(rates):
            for right in rates[i + 1 :]:
                same = (left.provider, left.model, left.operation, left.usage_unit) == (
                    right.provider,
                    right.model,
                    right.operation,
                    right.usage_unit,
                )
                overlaps = left.effective_start < (
                    right.effective_end or datetime.max.replace(tzinfo=left.effective_start.tzinfo)
                ) and right.effective_start < (
                    left.effective_end or datetime.max.replace(tzinfo=right.effective_start.tzinfo)
                )
                if same and left.active and right.active and overlaps:
                    raise ValueError("overlapping active pricing rates")

    def select(
        self, provider: str, model: str, operation: str, unit: UsageUnit, at: datetime
    ) -> PricingRate | None:
        return next(
            (
                r
                for r in self.version.rates
                if r.active
                and (r.provider, r.model, r.operation, r.usage_unit)
                == (provider, model, operation, unit)
                and r.effective_start <= at
                and (r.effective_end is None or at < r.effective_end)
            ),
            None,
        )
