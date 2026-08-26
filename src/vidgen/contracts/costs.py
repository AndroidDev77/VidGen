"""Deterministic pricing and financial contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from vidgen.contracts.telemetry import FrozenContract, UsageQuantity, UsageUnit


class BudgetDecision(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_WITH_WARNING = "ALLOW_WITH_WARNING"
    DENY_HARD_CAP = "DENY_HARD_CAP"
    DENY_ENTITY_CAP = "DENY_ENTITY_CAP"
    UNKNOWN_PRICE_REVIEW = "UNKNOWN_PRICE_REVIEW"


class LedgerStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    PARTIALLY_RECONCILED = "PARTIALLY_RECONCILED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


class PricingRate(FrozenContract):
    pricing_version_id: UUID
    provider: str
    model: str
    operation: str
    usage_unit: UsageUnit
    unit_size: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    effective_start: datetime
    effective_end: datetime | None = None
    source_reference: str
    verification_date: date
    active: bool = True
    notes: str = ""


class PricingCatalogVersion(FrozenContract):
    id: UUID
    name: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    rates: tuple[PricingRate, ...]


class CostLineItem(FrozenContract):
    usage: UsageQuantity
    rate: PricingRate
    amount: Decimal = Field(ge=0)


class CostEstimate(FrozenContract):
    line_items: tuple[CostLineItem, ...]
    estimated_cost: Decimal = Field(ge=0)
    actual_cost: Decimal | None = Field(None, ge=0)
    currency: str
    pricing_version_id: UUID | None
    warnings: tuple[str, ...] = ()


class BudgetPolicy(FrozenContract):
    version: str
    warning_cap: Decimal = Field(ge=0)
    hard_cap: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    entity_cap: Decimal | None = Field(None, ge=0)


class BudgetDecisionResult(FrozenContract):
    decision: BudgetDecision
    remaining_amount: Decimal
    warnings: tuple[str, ...] = ()


class CostReservationRequest(FrozenContract):
    project_id: UUID
    provider_attempt_id: UUID
    idempotency_key: str
    estimated_amount: Decimal = Field(ge=0)
    currency: str


class CostReservationResult(FrozenContract):
    reservation_id: UUID | None
    decision: BudgetDecision
    reserved_amount: Decimal = Field(ge=0)
    reused: bool = False


class CostReconciliationRequest(FrozenContract):
    reservation_id: UUID
    idempotency_key: str
    actual_amount: Decimal = Field(ge=0)
    billable: bool = True


class CostReconciliationResult(FrozenContract):
    ledger_entry_id: UUID
    committed_amount: Decimal = Field(ge=0)
    released_amount: Decimal = Field(ge=0)
    reused: bool = False


class CostLedgerEntry(FrozenContract):
    id: UUID
    project_id: UUID
    provider_attempt_id: UUID
    provider: str
    model: str
    operation: str
    reason: str
    pricing_version_id: UUID | None
    currency: str
    estimated_amount: Decimal = Field(ge=0)
    reserved_amount: Decimal = Field(ge=0)
    actual_amount: Decimal = Field(ge=0)
    released_amount: Decimal = Field(ge=0)
    status: LedgerStatus
    idempotency_key: str
    created_at: datetime


class ProjectCostSummary(FrozenContract):
    project_id: UUID
    warning_cap: Decimal
    hard_cap: Decimal
    reserved_amount: Decimal
    committed_amount: Decimal
    released_amount: Decimal
    remaining_amount: Decimal
    warning_percentage: Decimal | None
    hard_percentage: Decimal | None
    by_provider: dict[str, Decimal] = Field(default_factory=dict)
    by_model: dict[str, Decimal] = Field(default_factory=dict)
    by_operation: dict[str, Decimal] = Field(default_factory=dict)
    by_reason: dict[str, Decimal] = Field(default_factory=dict)
