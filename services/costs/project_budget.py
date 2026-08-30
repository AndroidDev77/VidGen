"""Project budget setup for the existing T23 cost ledger.

Every paid stage reserves against ``ProjectBudget`` before it calls a provider,
and ``CostRepository.reserve`` raises ``project budget not configured`` when the
row is absent. Project creation never wrote one, so a real-provider run died at
its first paid activity with a lookup error rather than a refusal an owner could
read. This module is the supported path that creates that row with the project,
inside the same transaction.

It adds no second budget system: it writes the T23 row, with the T23 currency
and policy version, and leaves reservation, reconciliation and the ledger
exactly where they already live.

Two rules shape the validation below:

* **Money is exact.** Caps arrive as decimal strings and are stored in the
  ledger's ``NUMERIC(18, 6)``. A value that would not survive that column is
  refused rather than silently rounded, because a rounded cap is a cap nobody
  agreed to.
* **A paid deployment needs a real ceiling.** A zero hard cap means "spend
  nothing", which is correct for a fake-provider run and useless for a real one:
  every reservation would be denied at the first activity. A deployment with a
  provider credential configured must name a positive hard cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.cost_models import ProjectBudget
from vidgen.db.models import Project

#: The ledger's currency. T23 stores one currency per budget and
#: ``CostRepository.reserve`` refuses a reservation that does not match it.
BUDGET_CURRENCY = "USD"

#: The budget-policy version this repository writes, matching the T23 naming
#: used by the rest of the cost stack.
BUDGET_POLICY_VERSION = "t23/1"

#: ``ProjectBudget`` money columns are ``NUMERIC(18, 6)``: at most six decimal
#: places, and at most eighteen digits in total once scaled to those places.
MONEY_DECIMAL_PLACES = 6
MONEY_TOTAL_DIGITS = 18
#: The exclusive upper bound, expressed the way the column stores the value: an
#: integer count of millionths that has to fit in eighteen digits. Comparing
#: against this catches an exponent-form amount such as ``1E+20``, which has one
#: significant digit and would otherwise reach PostgreSQL and overflow there.
MONEY_LIMIT = Decimal(10) ** (MONEY_TOTAL_DIGITS - MONEY_DECIMAL_PLACES)


class BudgetError(RuntimeError):
    """A structured, owner-renderable budget failure."""

    def __init__(self, code: str, summary: str, *, field: str | None = None) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.field = field


@dataclass(frozen=True, slots=True)
class BudgetDeployment:
    """Whether this deployment can spend real money.

    Derived from configuration rather than from anything an owner sends, and
    deliberately matching how the worker picks its providers: a configured
    OpenAI or Runway credential outranks the fake provider, so a project on such
    a deployment will make paid calls whatever ``allow_fake_providers`` says.
    """

    paid_provider_configured: bool

    @classmethod
    def from_settings(cls, settings: Any) -> BudgetDeployment:
        return cls(
            paid_provider_configured=bool(
                getattr(settings, "openai_api_key", None)
                or getattr(settings, "runway_api_secret", None)
            )
        )


def parse_amount(value: object, *, field: str) -> Decimal:
    """Read one exact decimal amount, refusing anything the ledger cannot hold.

    ``float`` is rejected outright: a binary float has already lost the value
    the owner typed by the time it reaches here, and the ledger's whole point is
    that no currency amount is ever approximated.
    """
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, bool) or isinstance(value, float):
        raise BudgetError(
            "budget_amount_invalid",
            'Provide the amount as an exact decimal string, for example "25.00".',
            field=field,
        )
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except (InvalidOperation, ValueError) as error:
            raise BudgetError(
                "budget_amount_invalid",
                "That is not a valid decimal amount.",
                field=field,
            ) from error
    else:
        raise BudgetError(
            "budget_amount_invalid",
            "That is not a valid decimal amount.",
            field=field,
        )
    if not amount.is_finite():
        raise BudgetError(
            "budget_amount_invalid",
            "That is not a valid decimal amount.",
            field=field,
        )
    if amount < 0:
        raise BudgetError("budget_amount_negative", "A budget cap cannot be negative.", field=field)
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)  # finite, so never "n"/"N"/"F"
    if -exponent > MONEY_DECIMAL_PLACES:
        raise BudgetError(
            "budget_amount_precision",
            f"A budget cap supports at most {MONEY_DECIMAL_PLACES} decimal places.",
            field=field,
        )
    if amount >= MONEY_LIMIT:
        raise BudgetError(
            "budget_amount_too_large", "That budget cap is too large to store.", field=field
        )
    return amount


def stored_amount(amount: Decimal) -> str:
    """Render an amount at the ledger column's scale, exactly.

    ``NUMERIC(18, 6)`` returns six decimal places, so an amount that has been
    through the database and one that has not must be rendered the same way or
    the same budget reads differently depending on who is asking.
    """
    return str(amount.quantize(Decimal(1).scaleb(-MONEY_DECIMAL_PLACES)))


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    """One validated pair of caps, ready to persist."""

    warning_cap: Decimal
    hard_cap: Decimal


def validate_caps(
    warning_cap: object, hard_cap: object, deployment: BudgetDeployment
) -> BudgetCaps:
    """Validate the pair together, in the order an owner would read them."""
    warning = parse_amount(warning_cap, field="budget_warning_cap")
    hard = parse_amount(hard_cap, field="budget_hard_cap")
    if hard < warning:
        raise BudgetError(
            "budget_hard_cap_below_warning",
            "The hard cap must be at least the warning cap.",
            field="budget_hard_cap",
        )
    if deployment.paid_provider_configured and hard <= 0:
        raise BudgetError(
            "budget_hard_cap_required",
            "This deployment uses paid providers, so the project needs a positive hard cap.",
            field="budget_hard_cap",
        )
    return BudgetCaps(warning_cap=warning, hard_cap=hard)


def budget_for(session: Session, project_id: UUID) -> ProjectBudget | None:
    return session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project_id))


def create_budget(
    session: Session,
    project: Project,
    *,
    warning_cap: object,
    hard_cap: object,
    deployment: BudgetDeployment,
) -> ProjectBudget:
    """Create the project's T23 budget row.

    The caller owns the transaction: this flushes so the row is visible to the
    rest of the request, and commits nothing. A project and its budget are
    created together or not at all.
    """
    caps = validate_caps(warning_cap, hard_cap, deployment)
    if budget_for(session, project.id) is not None:
        raise BudgetError(
            "budget_already_exists", "This project already has a budget.", field="budget_hard_cap"
        )
    budget = ProjectBudget(
        project_id=project.id,
        warning_cap=caps.warning_cap,
        hard_cap=caps.hard_cap,
        currency=BUDGET_CURRENCY,
        policy_version=BUDGET_POLICY_VERSION,
        reserved_amount=Decimal("0"),
        committed_amount=Decimal("0"),
        released_amount=Decimal("0"),
        row_version=1,
    )
    session.add(budget)
    session.flush()
    return budget


def set_caps(
    session: Session,
    project: Project,
    *,
    warning_cap: object,
    hard_cap: object,
    deployment: BudgetDeployment,
) -> ProjectBudget:
    """Create or update the project's caps, keeping every recorded amount.

    A project created before budgets were required has no row at all, and one
    created with a zero cap on a fake deployment needs a real one before it can
    run for real. Both are the same operation: the caps move, and the reserved,
    committed and released totals the ledger owns are never touched by it.

    Lowering the cap below what is already reserved or committed is refused
    rather than silently applied: the ledger would have no consistent state to
    reconcile into, and money already spent cannot be un-spent by a form.
    """
    caps = validate_caps(warning_cap, hard_cap, deployment)
    budget = budget_for(session, project.id)
    if budget is None:
        return create_budget(
            session,
            project,
            warning_cap=caps.warning_cap,
            hard_cap=caps.hard_cap,
            deployment=deployment,
        )
    spent = budget.committed_amount + budget.reserved_amount
    if caps.hard_cap < spent:
        raise BudgetError(
            "budget_hard_cap_below_spend",
            f"This project has already committed or reserved {spent}, so its hard cap "
            "cannot be lowered below that.",
            field="budget_hard_cap",
        )
    budget.warning_cap = caps.warning_cap
    budget.hard_cap = caps.hard_cap
    budget.row_version += 1
    session.flush()
    return budget


def startable(budget: ProjectBudget | None, deployment: BudgetDeployment) -> bool:
    """Whether a workflow may start against this budget.

    A fake-provider deployment spends nothing, so a zero-dollar budget - or the
    absence of one - is not a reason to refuse it. A paid deployment needs a
    budget row with a positive hard cap and headroom left under it, because
    every paid activity reserves against exactly those numbers.
    """
    if not deployment.paid_provider_configured:
        return True
    if budget is None or budget.hard_cap <= 0:
        return False
    return budget.committed_amount + budget.reserved_amount < budget.hard_cap
