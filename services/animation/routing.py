"""Versioned deterministic Runway model routing.

One policy decides which model every video generation uses, whether the call is
the first T15 attempt for a shot or a bounded T21 quality-repair escalation. The
decision is a pure function of ``RoutingContext``: the same context always yields
the same ``RoutingDecision``, and the decision carries a bounded reason so the
choice is durable and auditable next to the provider attempt it produced.

The rules, in the order they are applied:

* A request the selected model cannot serve - an unknown model, an unaccepted
  duration or ratio - is a deterministic configuration failure. It is never
  sent and never retried.
* An explicitly requested model wins where the quality mode permits it.
  ``economy`` never permits Gen-4.5. ``balanced`` permits it only for a hero
  shot or an eligible quality-repair escalation. ``premium`` permits it for any
  compatible shot, hero or not.
* ``economy`` routes every shot to Gen-4 Turbo.
* ``balanced`` routes to Gen-4 Turbo, upgrading a hero shot or an eligible
  escalation to Gen-4.5 when the model is available and the remaining budget
  covers the estimate. An escalation is eligible only while the existing T21
  retry policy still allows another same-provider repair.
* ``premium`` routes every compatible shot to Gen-4.5 and refuses the run with
  an actionable error when the model is unavailable or unaffordable, unless
  the project explicitly allows a Gen-4 Turbo fallback.
* Whatever was selected must fit under the project's hard cap; a budget the
  selection cannot meet is refused before any reservation is attempted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from services.animation.pricing import estimate_runway_cost
from services.animation.providers import CAPABILITIES, VideoCapability, capability_for
from vidgen.contracts.animation import RunwayModel
from vidgen.contracts.generation import GenerationQuality, RoutingDecision, RoutingReasonCode

ROUTING_POLICY_VERSION = "runway-routing-v2"
#: The bounded escalation policy: a balanced project may upgrade a shot to
#: Gen-4.5 only inside the existing T21 same-provider repair allowance.
QUALITY_REPAIR_POLICY_VERSION = "quality-repair/1"
DEFAULT_MAX_ESCALATION_ATTEMPTS = 2
PREMIUM_MODEL = RunwayModel.GEN4_5
ECONOMY_MODEL = RunwayModel.GEN4_TURBO


class RoutingError(ValueError):
    """A routing refusal. Every subclass is deterministic and non-retryable."""

    code: str = "routing_refused"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DeterministicConfigurationFailure(RoutingError):
    code = "deterministic_configuration_failure"


class UnsupportedCapability(RoutingError):
    code = "unsupported_capability"


class BudgetDenied(RoutingError):
    code = "budget_denied"


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Everything the router may consider, and nothing else."""

    quality_mode: GenerationQuality = GenerationQuality.BALANCED
    requested_model: RunwayModel | None = None
    hero_shot: bool = False
    #: True only for a T21 repair after a visual-quality failure. Deterministic
    #: configuration failures never set it.
    quality_escalation: bool = False
    #: The ordinal of the same-provider repair this escalation would be; zero
    #: for an ordinary first generation.
    escalation_attempt: int = 0
    max_escalation_attempts: int = DEFAULT_MAX_ESCALATION_ATTEMPTS
    requested_duration_seconds: float = 2
    width: int = 1280
    height: int = 720
    #: Whether the selected model's documented ratio and duration limits are
    #: enforced. The deterministic fake provider renders any geometry, so the
    #: repair and test paths that drive it disable the check; every Runway call
    #: keeps it.
    capability_enforced: bool = True
    #: Whether a T23 budget governs this provider. A fake provider spends
    #: nothing, so nothing is checked.
    budget_enforced: bool = False
    remaining_budget: Decimal | None = None
    hard_cap: Decimal | None = None
    premium_fallback_allowed: bool = False
    available_models: frozenset[RunwayModel] = frozenset(RunwayModel)
    attempt_number: int = 1
    prior_models: tuple[str, ...] = ()
    #: The T13 capability profile the storyboard was planned against.
    capability_profile_id: str = "runway-gen4-turbo"
    capability_hash: str = "0" * 64


def _compatible(capability: VideoCapability, context: RoutingContext) -> str | None:
    """None when the model can serve the shot, otherwise the reason it cannot."""
    if RunwayModel(capability.model) not in context.available_models:
        return f"{capability.display_name} is not available on this deployment"
    if not context.capability_enforced:
        return None
    if not capability.image_to_video:
        return f"{capability.display_name} does not accept image-to-video requests"
    if capability.select_duration(context.requested_duration_seconds) is None:
        return (
            f"{capability.display_name} accepts {capability.min_duration_seconds}-"
            f"{capability.max_duration_seconds} whole seconds, not "
            f"{context.requested_duration_seconds}"
        )
    if not capability.supports_dimensions(context.width, context.height):
        return f"{capability.display_name} does not output {context.width}:{context.height}"
    return None


def _estimate(capability: VideoCapability, context: RoutingContext) -> Decimal:
    duration = capability.select_duration(context.requested_duration_seconds)
    return estimate_runway_cost(capability.model, duration or context.requested_duration_seconds)


def _affordable(capability: VideoCapability, context: RoutingContext) -> str | None:
    """None when the budget covers the estimate, otherwise an actionable reason."""
    if not context.budget_enforced:
        return None
    estimate = _estimate(capability, context)
    remaining = context.remaining_budget if context.remaining_budget is not None else Decimal(0)
    cap = context.hard_cap if context.hard_cap is not None else remaining
    if estimate > cap:
        return (
            f"{capability.display_name} needs {estimate} USD for this shot but the project's "
            f"hard cap is {cap} USD; raise the hard cap"
        )
    if estimate > remaining:
        return (
            f"{capability.display_name} needs {estimate} USD for this shot but only "
            f"{remaining} USD remains under the {cap} USD hard cap; raise the hard cap "
            "or wait for reservations to release"
        )
    return None


def _escalation_eligible(context: RoutingContext) -> bool:
    return context.quality_escalation and 1 <= context.escalation_attempt <= max(
        0, context.max_escalation_attempts
    )


def route_model(context: RoutingContext, requested: RunwayModel | None = None) -> RoutingDecision:
    """Select the model for one generation and explain the selection."""
    requested = requested if requested is not None else context.requested_model
    premium = capability_for(PREMIUM_MODEL)
    economy = capability_for(ECONOMY_MODEL)
    candidates: list[str] = []
    if requested is not None:
        selected, code, reason = _route_explicit(requested, context, candidates)
    elif context.quality_mode is GenerationQuality.ECONOMY:
        candidates.append(economy.model)
        selected, code, reason = (
            economy,
            RoutingReasonCode.ECONOMY_TURBO,
            "economy mode animates every shot with Gen-4 Turbo",
        )
    elif context.quality_mode is GenerationQuality.PREMIUM:
        selected, code, reason = _route_premium(premium, economy, context, candidates)
    else:
        selected, code, reason = _route_balanced(premium, economy, context, candidates)
    incompatible = _compatible(selected, context)
    if incompatible is not None:
        raise UnsupportedCapability(incompatible)
    unaffordable = _affordable(selected, context)
    if unaffordable is not None:
        raise BudgetDenied(unaffordable)
    duration = selected.select_duration(context.requested_duration_seconds) or max(
        1, ceil(Decimal(str(context.requested_duration_seconds)))
    )
    return RoutingDecision(
        routing_policy_version=ROUTING_POLICY_VERSION,
        quality_repair_policy_version=QUALITY_REPAIR_POLICY_VERSION,
        quality_mode=context.quality_mode,
        selected_model=selected.model,
        requested_model=requested.value if requested is not None else None,
        hero_shot=context.hero_shot,
        quality_escalation=context.quality_escalation,
        escalation_attempt=context.escalation_attempt,
        attempt_number=max(1, context.attempt_number),
        prior_models=list(context.prior_models)[:16],
        reason_code=code,
        reason=reason[:255],
        requested_duration_seconds=context.requested_duration_seconds,
        generation_duration_seconds=duration,
        width=context.width,
        height=context.height,
        estimated_cost=str(_estimate(selected, context)),
        budget_enforced=context.budget_enforced,
        remaining_budget=(
            str(context.remaining_budget) if context.remaining_budget is not None else None
        ),
        capability_profile_id=context.capability_profile_id,
        capability_hash=context.capability_hash,
        candidates=candidates[:8],
    )


def _route_explicit(
    requested: RunwayModel, context: RoutingContext, candidates: list[str]
) -> tuple[VideoCapability, RoutingReasonCode, str]:
    if requested.value not in CAPABILITIES:
        raise DeterministicConfigurationFailure(f"unknown Runway model {requested.value!r}")
    capability = capability_for(requested)
    candidates.append(capability.model)
    incompatible = _compatible(capability, context)
    if incompatible is not None:
        raise UnsupportedCapability(f"explicitly requested {incompatible}")
    if requested is PREMIUM_MODEL:
        if context.quality_mode is GenerationQuality.ECONOMY:
            raise DeterministicConfigurationFailure(
                "premium_model_not_permitted: economy mode never uses Gen-4.5; "
                "switch the project to balanced or premium"
            )
        if context.quality_mode is GenerationQuality.BALANCED and not (
            context.hero_shot or _escalation_eligible(context)
        ):
            raise DeterministicConfigurationFailure(
                "premium_model_not_permitted: balanced mode uses Gen-4.5 only for hero shots "
                "or an eligible quality-repair escalation; mark the shot as a hero shot or "
                "switch the project to premium"
            )
        if context.quality_mode is GenerationQuality.PREMIUM and context.premium_fallback_allowed:
            unaffordable = _affordable(capability, context)
            if unaffordable is not None:
                return (
                    capability_for(ECONOMY_MODEL),
                    RoutingReasonCode.PREMIUM_FALLBACK_BUDGET,
                    f"explicit Gen-4.5 request fell back to Gen-4 Turbo: {unaffordable}",
                )
    return (
        capability,
        RoutingReasonCode.EXPLICIT_MODEL,
        f"{capability.display_name} was explicitly requested and the "
        f"{context.quality_mode.value} mode permits it",
    )


def _route_premium(
    premium: VideoCapability,
    economy: VideoCapability,
    context: RoutingContext,
    candidates: list[str],
) -> tuple[VideoCapability, RoutingReasonCode, str]:
    candidates.append(premium.model)
    incompatible = _compatible(premium, context)
    if incompatible is not None:
        if context.premium_fallback_allowed:
            candidates.append(economy.model)
            return (
                economy,
                RoutingReasonCode.PREMIUM_FALLBACK_CAPABILITY,
                f"premium fallback to Gen-4 Turbo: {incompatible}",
            )
        raise UnsupportedCapability(
            f"premium mode cannot animate this shot: {incompatible}. Enable "
            "premium_fallback_allowed to animate it with Gen-4 Turbo instead"
        )
    unaffordable = _affordable(premium, context)
    if unaffordable is not None:
        if context.premium_fallback_allowed:
            candidates.append(economy.model)
            return (
                economy,
                RoutingReasonCode.PREMIUM_FALLBACK_BUDGET,
                f"premium fallback to Gen-4 Turbo: {unaffordable}",
            )
        raise BudgetDenied(
            f"premium mode cannot afford this shot: {unaffordable}, or enable "
            "premium_fallback_allowed to animate it with Gen-4 Turbo"
        )
    return (
        premium,
        RoutingReasonCode.PREMIUM_GEN4_5,
        "premium mode animates every compatible shot with Gen-4.5",
    )


def _route_balanced(
    premium: VideoCapability,
    economy: VideoCapability,
    context: RoutingContext,
    candidates: list[str],
) -> tuple[VideoCapability, RoutingReasonCode, str]:
    escalation = _escalation_eligible(context)
    if context.quality_escalation and not escalation:
        candidates.append(economy.model)
        return (
            economy,
            RoutingReasonCode.BALANCED_ESCALATION_EXHAUSTED,
            "quality-repair escalation exhausted its bounded attempts; Gen-4 Turbo continues",
        )
    if not (context.hero_shot or escalation):
        candidates.append(economy.model)
        return (
            economy,
            RoutingReasonCode.BALANCED_DEFAULT_TURBO,
            "balanced mode animates ordinary shots with Gen-4 Turbo",
        )
    candidates.append(premium.model)
    incompatible = _compatible(premium, context)
    if incompatible is not None:
        candidates.append(economy.model)
        return (
            economy,
            (
                RoutingReasonCode.BALANCED_ESCALATION_CAPABILITY_UNAVAILABLE
                if escalation
                else RoutingReasonCode.BALANCED_HERO_CAPABILITY_UNAVAILABLE
            ),
            f"Gen-4 Turbo selected because {incompatible}",
        )
    unaffordable = _affordable(premium, context)
    if unaffordable is not None:
        candidates.append(economy.model)
        return (
            economy,
            (
                RoutingReasonCode.BALANCED_ESCALATION_BUDGET_INSUFFICIENT
                if escalation
                else RoutingReasonCode.BALANCED_HERO_BUDGET_INSUFFICIENT
            ),
            f"Gen-4 Turbo selected because {unaffordable}",
        )
    if escalation:
        return (
            premium,
            RoutingReasonCode.BALANCED_QUALITY_ESCALATION,
            f"quality-repair escalation {context.escalation_attempt} of "
            f"{context.max_escalation_attempts} upgrades the shot to Gen-4.5",
        )
    return (
        premium,
        RoutingReasonCode.BALANCED_HERO_PREMIUM,
        "balanced mode upgrades this hero shot to Gen-4.5 within the remaining budget",
    )


def selected_model(decision: RoutingDecision) -> RunwayModel:
    return RunwayModel(decision.selected_model)
