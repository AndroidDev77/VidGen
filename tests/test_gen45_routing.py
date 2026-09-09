"""The versioned routing policy: quality modes, hero shots, escalation and budgets."""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.animation.routing import (
    QUALITY_REPAIR_POLICY_VERSION,
    ROUTING_POLICY_VERSION,
    BudgetDenied,
    DeterministicConfigurationFailure,
    RoutingContext,
    UnsupportedCapability,
    route_model,
)
from vidgen.contracts.animation import RunwayModel
from vidgen.contracts.generation import GenerationQuality, RoutingDecision, RoutingReasonCode

TURBO = RunwayModel.GEN4_TURBO.value
PREMIUM = RunwayModel.GEN4_5.value


def context(**overrides) -> RoutingContext:
    base = {
        "quality_mode": GenerationQuality.BALANCED,
        "requested_duration_seconds": 6.5,
        "budget_enforced": True,
        "remaining_budget": Decimal("50"),
        "hard_cap": Decimal("100"),
    }
    base.update(overrides)
    return RoutingContext(**base)


def test_economy_always_selects_turbo_even_for_hero_shots() -> None:
    decision = route_model(context(quality_mode=GenerationQuality.ECONOMY, hero_shot=True))
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.ECONOMY_TURBO
    assert decision.generation_duration_seconds == 7
    assert decision.estimated_cost == "0.350000"


def test_economy_refuses_an_explicit_premium_request_deterministically() -> None:
    with pytest.raises(
        DeterministicConfigurationFailure, match=r"economy mode never uses Gen-4\.5"
    ):
        route_model(context(quality_mode=GenerationQuality.ECONOMY), RunwayModel.GEN4_5)


def test_balanced_selects_turbo_for_ordinary_shots() -> None:
    decision = route_model(context())
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.BALANCED_DEFAULT_TURBO
    assert decision.hero_shot is False


def test_balanced_selects_premium_for_an_eligible_hero_shot() -> None:
    decision = route_model(context(hero_shot=True))
    assert decision.selected_model == PREMIUM
    assert decision.reason_code is RoutingReasonCode.BALANCED_HERO_PREMIUM
    assert decision.estimated_cost == "0.840000"
    assert decision.candidates == [PREMIUM]


def test_balanced_hero_shot_falls_back_to_turbo_when_the_budget_cannot_cover_premium() -> None:
    decision = route_model(context(hero_shot=True, remaining_budget=Decimal("0.50")))
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.BALANCED_HERO_BUDGET_INSUFFICIENT
    assert "0.840000" in decision.reason


def test_balanced_hero_shot_falls_back_when_premium_is_unavailable() -> None:
    decision = route_model(
        context(hero_shot=True, available_models=frozenset({RunwayModel.GEN4_TURBO}))
    )
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.BALANCED_HERO_CAPABILITY_UNAVAILABLE


def test_balanced_quality_escalation_is_bounded_by_the_repair_policy() -> None:
    first = route_model(context(quality_escalation=True, escalation_attempt=1))
    assert first.selected_model == PREMIUM
    assert first.reason_code is RoutingReasonCode.BALANCED_QUALITY_ESCALATION
    second = route_model(context(quality_escalation=True, escalation_attempt=2))
    assert second.selected_model == PREMIUM
    exhausted = route_model(context(quality_escalation=True, escalation_attempt=3))
    assert exhausted.selected_model == TURBO
    assert exhausted.reason_code is RoutingReasonCode.BALANCED_ESCALATION_EXHAUSTED
    # A deterministic configuration failure is never an escalation: without the
    # visual-quality flag the shot routes as an ordinary shot.
    ordinary = route_model(context(escalation_attempt=1))
    assert ordinary.reason_code is RoutingReasonCode.BALANCED_DEFAULT_TURBO


def test_balanced_escalation_respects_the_budget() -> None:
    decision = route_model(
        context(quality_escalation=True, escalation_attempt=1, remaining_budget=Decimal("0.60"))
    )
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.BALANCED_ESCALATION_BUDGET_INSUFFICIENT


def test_balanced_explicit_premium_needs_a_hero_shot_or_an_escalation() -> None:
    with pytest.raises(DeterministicConfigurationFailure, match="hero shots"):
        route_model(context(), RunwayModel.GEN4_5)
    hero = route_model(context(hero_shot=True), RunwayModel.GEN4_5)
    assert hero.selected_model == PREMIUM
    assert hero.reason_code is RoutingReasonCode.EXPLICIT_MODEL


def test_premium_selects_gen4_5_for_every_compatible_shot() -> None:
    decision = route_model(context(quality_mode=GenerationQuality.PREMIUM))
    assert decision.selected_model == PREMIUM
    assert decision.reason_code is RoutingReasonCode.PREMIUM_GEN4_5
    assert decision.hero_shot is False


def test_premium_honours_an_explicit_request_without_a_hero_designation() -> None:
    premium = route_model(context(quality_mode=GenerationQuality.PREMIUM), RunwayModel.GEN4_5)
    assert premium.selected_model == PREMIUM
    assert premium.reason_code is RoutingReasonCode.EXPLICIT_MODEL
    turbo = route_model(context(quality_mode=GenerationQuality.PREMIUM), RunwayModel.GEN4_TURBO)
    assert turbo.selected_model == TURBO
    assert turbo.reason_code is RoutingReasonCode.EXPLICIT_MODEL


def test_premium_refuses_the_run_with_an_actionable_error_when_unaffordable() -> None:
    with pytest.raises(BudgetDenied) as denied:
        route_model(
            context(quality_mode=GenerationQuality.PREMIUM, remaining_budget=Decimal("0.5"))
        )
    message = str(denied.value)
    assert "0.840000" in message
    assert "raise the hard cap" in message
    assert "premium_fallback_allowed" in message
    with pytest.raises(BudgetDenied, match=r"hard cap is 0\.10"):
        route_model(
            context(
                quality_mode=GenerationQuality.PREMIUM,
                remaining_budget=Decimal("0.10"),
                hard_cap=Decimal("0.10"),
            )
        )


def test_premium_falls_back_only_when_explicitly_configured() -> None:
    decision = route_model(
        context(
            quality_mode=GenerationQuality.PREMIUM,
            remaining_budget=Decimal("0.5"),
            premium_fallback_allowed=True,
        )
    )
    assert decision.selected_model == TURBO
    assert decision.reason_code is RoutingReasonCode.PREMIUM_FALLBACK_BUDGET
    unavailable = route_model(
        context(
            quality_mode=GenerationQuality.PREMIUM,
            available_models=frozenset({RunwayModel.GEN4_TURBO}),
            premium_fallback_allowed=True,
        )
    )
    assert unavailable.reason_code is RoutingReasonCode.PREMIUM_FALLBACK_CAPABILITY
    with pytest.raises(UnsupportedCapability, match="not available"):
        route_model(
            context(
                quality_mode=GenerationQuality.PREMIUM,
                available_models=frozenset({RunwayModel.GEN4_TURBO}),
            )
        )


def test_unsupported_geometry_or_duration_is_a_deterministic_refusal() -> None:
    with pytest.raises(UnsupportedCapability, match="does not output 1080:1920"):
        route_model(context(width=1080, height=1920))
    with pytest.raises(UnsupportedCapability, match=r"not 12\.0"):
        route_model(context(requested_duration_seconds=12.0))
    with pytest.raises(UnsupportedCapability, match="explicitly requested"):
        route_model(context(requested_duration_seconds=12.0), RunwayModel.GEN4_TURBO)


def test_a_turbo_selection_that_exceeds_the_hard_cap_is_refused_before_dispatch() -> None:
    with pytest.raises(BudgetDenied):
        route_model(context(quality_mode=GenerationQuality.ECONOMY, remaining_budget=Decimal("0")))


def test_routing_is_deterministic_and_records_a_bounded_reason() -> None:
    first = route_model(context(hero_shot=True, attempt_number=2, prior_models=("gen4_turbo",)))
    second = route_model(context(hero_shot=True, attempt_number=2, prior_models=("gen4_turbo",)))
    assert first == second
    assert first.routing_policy_version == ROUTING_POLICY_VERSION
    assert first.quality_repair_policy_version == QUALITY_REPAIR_POLICY_VERSION
    assert first.attempt_number == 2
    assert first.prior_models == ["gen4_turbo"]
    assert len(first.reason) <= 255
    # The decision round-trips through its strict contract unchanged.
    assert RoutingDecision.model_validate(first.model_dump(mode="json")) == first
    with pytest.raises(ValueError):
        RoutingDecision.model_validate({**first.model_dump(mode="json"), "prompt": "leak"})


def test_a_fake_provider_is_never_budget_checked() -> None:
    decision = route_model(RoutingContext(quality_mode=GenerationQuality.PREMIUM))
    assert decision.selected_model == PREMIUM
    assert decision.budget_enforced is False
    assert decision.remaining_budget is None
