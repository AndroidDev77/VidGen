"""Versioned deterministic Runway model routing."""

from __future__ import annotations

from dataclasses import dataclass

from vidgen.contracts.animation import RunwayModel

ROUTING_POLICY_VERSION = "runway-routing-v1"


@dataclass(frozen=True, slots=True)
class RoutingContext:
    hero_shot: bool = False
    premium_permitted: bool = False
    premium_budget_available: bool = False
    premium_capability_available: bool = True


def route_model(context: RoutingContext, requested: RunwayModel | None = None) -> RunwayModel:
    if requested == RunwayModel.GEN4_5 and not all(
        (
            context.hero_shot,
            context.premium_permitted,
            context.premium_budget_available,
            context.premium_capability_available,
        )
    ):
        raise ValueError("premium_model_not_permitted")
    if requested is not None:
        return requested
    if all(
        (
            context.hero_shot,
            context.premium_permitted,
            context.premium_budget_available,
            context.premium_capability_available,
        )
    ):
        return RunwayModel.GEN4_5
    return RunwayModel.GEN4_TURBO
