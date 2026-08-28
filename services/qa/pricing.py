"""Deterministic T20 visual-QA cost estimation.

Estimates are per-attempt and bounded by the evidence actually sent: a base
request charge plus a per-frame charge. They feed the T23 budget reservation
before any paid call and are reconciled against the usage the adapter reports.
The rates are configuration, not a provider price list; T23's pricing catalog
remains the authority for a configured production provider.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

VISUAL_QA_PRICING_VERSION: Final = "visual-qa-pricing/1.0"
BASE_REQUEST_COST: Final = Decimal("0.0100")
PER_FRAME_COST: Final = Decimal("0.0025")
PER_REFERENCE_COST: Final = Decimal("0.0015")


def estimate_visual_qa_cost(*, frames: int, references: int) -> Decimal:
    """Return the estimated USD cost of one bounded visual-agent evaluation."""
    if frames < 0 or references < 0:
        raise ValueError("frame and reference counts must be non-negative")
    return (BASE_REQUEST_COST + PER_FRAME_COST * frames + PER_REFERENCE_COST * references).quantize(
        Decimal("0.000001")
    )


def to_microusd(amount: Decimal) -> int:
    return int((amount * 1_000_000).to_integral_value())
