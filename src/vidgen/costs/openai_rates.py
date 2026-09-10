"""Published OpenAI list prices, used when the pricing catalog has no rate.

``provider_price_rates`` is the authoritative source of truth for money, and
nothing in this repository seeds it yet. Until an operator loads a dated,
sourced catalog, a paid call would otherwise reconcile at zero and a project
would report no spend at all. This table is the stand-in: real published list
prices, quoted per million tokens, so an unseeded deployment reports the right
order of magnitude instead of nothing.

It is deliberately not a catalog. There is no effective-date interval, no
pricing version and no source URL behind each row, so a call priced from here
is marked ``fallback`` rather than ``catalog`` wherever it is recorded, and any
seeded rate wins over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: List prices are quoted per million tokens.
TOKENS_PER_PRICED_UNIT = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class TokenRates:
    """One model tier's list price, in dollars per million tokens."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


#: Keyed by the most specific model name each tier is published under.
RATES: dict[str, TokenRates] = {
    "gpt-6-astra": TokenRates(Decimal("10.00"), Decimal("1.00"), Decimal("50.00")),
    # Sol's input price is a promotional rate and will move; the catalog, once
    # seeded, is where a dated price belongs.
    "gpt-5.6-sol": TokenRates(Decimal("4.00"), Decimal("0.40"), Decimal("20.00")),
    "gpt-5.6-terra": TokenRates(Decimal("2.00"), Decimal("0.20"), Decimal("12.00")),
    "gpt-4o": TokenRates(Decimal("2.50"), Decimal("1.25"), Decimal("10.00")),
    "gpt-4.1-mini": TokenRates(Decimal("0.40"), Decimal("0.10"), Decimal("1.60")),
    "o3": TokenRates(Decimal("0.40"), Decimal("0.10"), Decimal("1.60")),
    "gpt-5.6-luna": TokenRates(Decimal("0.20"), Decimal("0.02"), Decimal("1.20")),
    "gpt-4o-mini": TokenRates(Decimal("0.15"), Decimal("0.075"), Decimal("0.60")),
    "gpt-5-nano": TokenRates(Decimal("0.05"), Decimal("0.005"), Decimal("0.40")),
}

#: Names that do not identify a tier on their own. Every model setting in this
#: repository is the bare ``gpt-5.6``, which spans three tiers a factor of
#: twenty apart, so it resolves to the middle one rather than reporting nothing.
#: A deployment that runs a different tier should say so in its model setting.
ALIASES: dict[str, str] = {
    "gpt-5.6": "gpt-5.6-terra",
    "gpt-6": "gpt-6-astra",
}


def resolve(model: str) -> str | None:
    """Return the tier a model name is priced under, or None if it names none.

    Matching is longest-prefix, so a dated snapshot (``gpt-4o-2026-05-01``)
    prices as its family and a more specific tier always wins over a shorter
    name that is a prefix of it (``gpt-4o-mini`` over ``gpt-4o``).
    """
    name = model.strip().lower()
    if name in ALIASES:
        return ALIASES[name]
    if name in RATES:
        return name
    candidates = sorted(RATES | ALIASES, key=len, reverse=True)
    for candidate in candidates:
        if name.startswith(candidate):
            return ALIASES.get(candidate, candidate)
    return None


def unit_price(model: str, usage_unit: str) -> Decimal | None:
    """Price one token of ``usage_unit`` for ``model``, in dollars.

    Returns None when the model names no known tier or the unit is not a token
    unit, so the caller can tell "no published price" from "free".
    """
    tier = resolve(model)
    if tier is None:
        return None
    rates = RATES[tier]
    per_million = {
        "INPUT_TOKEN": rates.input,
        "CACHED_INPUT_TOKEN": rates.cached_input,
        "OUTPUT_TOKEN": rates.output,
    }.get(usage_unit)
    return None if per_million is None else per_million / TOKENS_PER_PRICED_UNIT
