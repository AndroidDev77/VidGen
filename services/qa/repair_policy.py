"""The bounded T21 repair policy.

One shot may cost at most:

* one original T15 generation (which T21 does not count as a repair),
* two same-provider repair generations,
* one alternate-provider generation,
* one deterministic 2.5D fallback render, which costs no provider charge.

The route order is fixed and total:

.. code-block:: text

    T20 QA failure
    -> classify failure
    -> same-provider repair 1  -> T20 QA
    -> same-provider repair 2  -> T20 QA
    -> one alternate-provider attempt -> T20 QA
    -> deterministic 2.5D fallback when eligible -> T20 QA
    -> otherwise HUMAN_REVIEW_REQUIRED

There is no loop back and no unbounded retry. Network polling, media download,
normalization and storage retries are not routes at all: when a durable provider
operation already exists the router returns ``RESUME_PROVIDER_OPERATION``, which
consumes no attempt and starts no new paid generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from vidgen.contracts.repair import (
    HumanReviewReason,
    RepairAttemptKind,
    RepairClassification,
    RepairFailureCategory,
    RepairPolicy,
    RepairRoute,
    RepairRunState,
)

POLICY_VERSION = "t21-repair-policy/1.0"
MAX_SAME_PROVIDER_REPAIRS = 2
MAX_ALTERNATE_PROVIDER_ATTEMPTS = 1
MAX_FALLBACK_RENDERS = 1
TARGETED_REPAIR_FLOOR = 75.0


def default_policy(
    *,
    max_same_provider_repairs: int = MAX_SAME_PROVIDER_REPAIRS,
    allow_parallax_fallback: bool = True,
    per_shot_repair_cost_limit: Decimal | None = None,
    per_run_repair_cost_limit: Decimal | None = None,
) -> RepairPolicy:
    """The repository's bounded policy, with only the operator knobs exposed.

    There is deliberately no numeric budget baked in here: money limits come
    from the project's configured T23 budget and the active pricing catalog, and
    the optional per-shot and per-run limits are configuration, not constants.
    """
    return RepairPolicy(
        policy_version=POLICY_VERSION,
        max_same_provider_repairs=max_same_provider_repairs,
        max_alternate_provider_attempts=MAX_ALTERNATE_PROVIDER_ATTEMPTS,
        max_fallback_renders=MAX_FALLBACK_RENDERS,
        allow_parallax_fallback=allow_parallax_fallback,
        targeted_repair_floor=TARGETED_REPAIR_FLOOR,
        per_shot_repair_cost_limit=per_shot_repair_cost_limit,
        per_run_repair_cost_limit=per_run_repair_cost_limit,
    )


@dataclass(frozen=True, slots=True)
class RouteContext:
    """Everything the router is allowed to consider, and nothing else."""

    classification: RepairClassification
    policy: RepairPolicy
    same_provider_repairs_used: int = 0
    alternate_provider_attempts_used: int = 0
    fallback_renders_used: int = 0
    #: A durable provider operation that already exists and is not terminal.
    resumable_operation: bool = False
    #: A provider operation that succeeded but whose output was never persisted.
    unpersisted_provider_output: bool = False
    #: A submission whose outcome we could not determine. Never resubmitted.
    ambiguous_submission: bool = False
    alternate_provider_available: bool = True
    fallback_eligible: bool = False
    fallback_ineligibility_reasons: tuple[str, ...] = ()
    budget_allows_next_attempt: bool = True
    budget_denial_reason: HumanReviewReason | None = None
    cancellation_requested: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One bounded routing decision and the reasons behind it."""

    route: RepairRoute
    attempt_kind: RepairAttemptKind | None
    state: RepairRunState
    rationale: tuple[str, ...]
    human_review_reason: HumanReviewReason | None = None
    #: Route consumes one of the policy's bounded attempts.
    consumes_attempt: bool = True
    warnings: tuple[str, ...] = field(default=())


def next_route(context: RouteContext) -> RouteDecision:
    """Choose the safest, least expensive valid recovery path.

    The order below is the whole policy. Every branch either resumes work
    already paid for, spends one bounded attempt, renders for free, or stops.
    """
    classification = context.classification
    policy = context.policy

    # 1. An operation we already own is never re-paid for. Resuming a running
    #    operation, or persisting output we already generated, consumes no
    #    attempt: it finishes work the project has already been charged for.
    if context.unpersisted_provider_output:
        return RouteDecision(
            route=RepairRoute.RESUME_PROVIDER_OPERATION,
            attempt_kind=None,
            state=RepairRunState.REVALIDATING,
            rationale=(
                "a provider operation completed but its output was never persisted; "
                "resuming it instead of paying for a second generation",
            ),
            consumes_attempt=False,
        )
    if context.resumable_operation:
        return RouteDecision(
            route=RepairRoute.RESUME_PROVIDER_OPERATION,
            attempt_kind=None,
            state=RepairRunState.REPAIRING,
            rationale=("a durable provider operation is still running; resuming its poll",),
            consumes_attempt=False,
        )

    # 2. Cancellation is honoured only between paid attempts, never mid-flight.
    if context.cancellation_requested:
        return _review(
            HumanReviewReason.CANCELLED_BEFORE_PAID_ATTEMPT,
            "cancellation was requested before the next paid attempt",
        )

    # 3. An ambiguous submission may already have been billed. Resubmitting is
    #    exactly the duplicate charge the policy exists to prevent.
    if context.ambiguous_submission:
        return _review(
            HumanReviewReason.DETERMINISTIC_FAILURE,
            "a provider submission outcome is unknown; reconcile it before spending again",
        )

    # 4. An approved reference that is itself wrong is an upstream problem. We
    #    never fabricate a replacement and never pay to regenerate against it.
    if classification.requires_upstream_reference_correction:
        return RouteDecision(
            route=RepairRoute.UPSTREAM_REFERENCE_CORRECTION,
            attempt_kind=None,
            state=RepairRunState.HUMAN_REVIEW_REQUIRED,
            rationale=(
                "T20 evidence shows the approved T19 reference itself is invalid; "
                "the correction belongs upstream, not in another generation",
            ),
            human_review_reason=HumanReviewReason.UPSTREAM_REFERENCE_CORRECTION,
            consumes_attempt=False,
        )

    # 5. A shot that cannot be produced as specified never earns a paid attempt.
    if classification.category is RepairFailureCategory.IMPOSSIBLE_SHOT:
        return _review(
            HumanReviewReason.IMPOSSIBLE_SHOT,
            "the shot requests a duration or motion no configured provider can produce",
        )

    # 6. Deterministic input, lineage, configuration and media-processing
    #    failures are ours, not the provider's. Money never fixes them.
    if classification.deterministic_only:
        return _review(
            HumanReviewReason.DETERMINISTIC_FAILURE,
            f"{classification.primary_code.value} is a deterministic failure; "
            "a paid generation cannot fix it",
        )

    # 7. Two same-provider repairs, then one alternate provider, then free.
    if context.same_provider_repairs_used < policy.max_same_provider_repairs:
        return _paid(
            context,
            RepairRoute.SAME_PROVIDER_REPAIR,
            RepairAttemptKind.SAME_PROVIDER_REPAIR,
            RepairRunState.REPAIRING,
            (
                f"same-provider repair "
                f"{context.same_provider_repairs_used + 1} of "
                f"{policy.max_same_provider_repairs}",
                f"{classification.severity.value} repair for {classification.primary_code.value}",
            ),
        )
    if (
        context.alternate_provider_attempts_used < policy.max_alternate_provider_attempts
        and context.alternate_provider_available
    ):
        return _paid(
            context,
            RepairRoute.ALTERNATE_PROVIDER,
            RepairAttemptKind.ALTERNATE_PROVIDER,
            RepairRunState.ALTERNATE_PROVIDER,
            (
                "both same-provider repairs are spent",
                "one alternate-provider attempt remains",
            ),
        )
    if (
        policy.allow_parallax_fallback
        and context.fallback_renders_used < policy.max_fallback_renders
        and context.fallback_eligible
    ):
        return RouteDecision(
            route=RepairRoute.DETERMINISTIC_FALLBACK,
            attempt_kind=RepairAttemptKind.DETERMINISTIC_FALLBACK,
            state=RepairRunState.FALLBACK_RENDERING,
            rationale=(
                "every paid attempt is spent",
                "the shot is eligible for a deterministic 2.5D render, which costs nothing",
            ),
        )

    # 8. Nothing bounded is left.
    if not context.fallback_eligible and policy.allow_parallax_fallback:
        return _review(
            HumanReviewReason.FALLBACK_INELIGIBLE,
            "no paid attempt remains and the shot cannot be represented by a 2.5D render: "
            + "; ".join(context.fallback_ineligibility_reasons[:4]),
        )
    return _review(
        HumanReviewReason.ATTEMPT_LIMIT_REACHED,
        "the bounded repair policy is exhausted",
    )


def _paid(
    context: RouteContext,
    route: RepairRoute,
    kind: RepairAttemptKind,
    state: RepairRunState,
    rationale: tuple[str, ...],
) -> RouteDecision:
    """A paid route is only chosen when the budget can actually cover it."""
    if not context.budget_allows_next_attempt:
        reason = context.budget_denial_reason or HumanReviewReason.REPAIR_BUDGET_EXHAUSTED
        return _review(
            reason,
            "the next paid attempt would exceed an applicable hard limit, so the provider "
            "is never called",
        )
    return RouteDecision(route=route, attempt_kind=kind, state=state, rationale=rationale)


def _review(reason: HumanReviewReason, explanation: str) -> RouteDecision:
    return RouteDecision(
        route=RepairRoute.HUMAN_REVIEW_REQUIRED,
        attempt_kind=None,
        state=RepairRunState.HUMAN_REVIEW_REQUIRED,
        rationale=(explanation,),
        human_review_reason=reason,
        consumes_attempt=False,
    )


__all__ = [
    "MAX_ALTERNATE_PROVIDER_ATTEMPTS",
    "MAX_FALLBACK_RENDERS",
    "MAX_SAME_PROVIDER_REPAIRS",
    "POLICY_VERSION",
    "TARGETED_REPAIR_FLOOR",
    "RouteContext",
    "RouteDecision",
    "default_policy",
    "next_route",
]
