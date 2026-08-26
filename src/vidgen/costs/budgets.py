from decimal import Decimal

from vidgen.contracts.costs import BudgetDecision, BudgetDecisionResult, BudgetPolicy


def decide_budget(
    policy: BudgetPolicy,
    committed: Decimal,
    reserved: Decimal,
    estimate: Decimal,
    *,
    unknown_price: bool = False,
) -> BudgetDecisionResult:
    remaining = policy.hard_cap - committed - reserved
    if unknown_price:
        decision = BudgetDecision.UNKNOWN_PRICE_REVIEW
    elif policy.entity_cap is not None and estimate > policy.entity_cap:
        decision = BudgetDecision.DENY_ENTITY_CAP
    elif estimate > remaining:
        decision = BudgetDecision.DENY_HARD_CAP
    elif committed + reserved + estimate >= policy.warning_cap:
        decision = BudgetDecision.ALLOW_WITH_WARNING
    else:
        decision = BudgetDecision.ALLOW
    return BudgetDecisionResult(
        decision=decision,
        remaining_amount=remaining,
        warnings=("WARNING_CAP",) if decision == BudgetDecision.ALLOW_WITH_WARNING else (),
    )
