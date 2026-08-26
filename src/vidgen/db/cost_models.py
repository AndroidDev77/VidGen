from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

Money = Numeric(18, 6)


class PricingVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pricing_versions"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_date: Mapped[date] = mapped_column(Date)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("length(currency)=3", name="currency_iso"),)


class ProviderPriceRate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_price_rates"
    pricing_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("pricing_versions.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64))
    usage_unit: Mapped[str] = mapped_column(String(32))
    unit_size: Mapped[Decimal] = mapped_column(Money)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    effective_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool]
    source_reference: Mapped[str] = mapped_column(String(1024))
    notes: Mapped[str] = mapped_column(String(1024), default="")
    __table_args__ = (
        CheckConstraint("unit_size>0 AND unit_price>=0", name="nonnegative_rate"),
        CheckConstraint(
            "effective_end IS NULL OR effective_end>effective_start", name="valid_interval"
        ),
        UniqueConstraint(
            "pricing_version_id", "provider", "model", "operation", "usage_unit", "effective_start"
        ),
    )


class ProviderAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_attempts"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    related_entity_type: Mapped[str | None] = mapped_column(String(64))
    related_entity_id: Mapped[UUID | None]
    operation: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int]
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_configuration_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    failure_class: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None]
    usage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    actual_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    pricing_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pricing_versions.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    latency_ms: Mapped[int | None]
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255))
    temporal_run_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redacted_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("attempt_number>0", name="positive_attempt"),
        CheckConstraint("estimated_cost>=0 AND actual_cost>=0", name="nonnegative_cost"),
        UniqueConstraint("project_id", "provider", "operation", "idempotency_key"),
        Index(
            "uq_provider_request_identity",
            "provider",
            "provider_request_id",
            unique=True,
            postgresql_where=text("provider_request_id IS NOT NULL"),
            sqlite_where=text("provider_request_id IS NOT NULL"),
        ),
    )


class ProjectBudget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_budgets"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    warning_cap: Mapped[Decimal] = mapped_column(Money)
    hard_cap: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    policy_version: Mapped[str] = mapped_column(String(64))
    reserved_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    committed_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    released_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint("warning_cap>=0 AND hard_cap>=warning_cap", name="valid_caps"),
        CheckConstraint(
            "reserved_amount>=0 AND committed_amount>=0 AND released_amount>=0",
            name="nonnegative_totals",
        ),
    )


class CostReservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cost_reservations"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    provider_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    estimated_amount: Mapped[Decimal] = mapped_column(Money)
    reserved_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(32))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "estimated_amount>=0 AND reserved_amount>=0", name="nonnegative_reservation"
        ),
    )


class CostLedgerEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cost_ledger_entries"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    provider_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="RESTRICT")
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_reservations.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    pricing_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pricing_versions.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    estimated_amount: Mapped[Decimal] = mapped_column(Money)
    reserved_amount: Mapped[Decimal] = mapped_column(Money)
    actual_amount: Mapped[Decimal] = mapped_column(Money)
    released_amount: Mapped[Decimal] = mapped_column(Money)
    usage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "estimated_amount>=0 AND reserved_amount>=0 "
            "AND actual_amount>=0 AND released_amount>=0",
            name="nonnegative_ledger",
        ),
    )


class PipelineFailureEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pipeline_failure_events"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[str] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(String(64))
    related_entity_type: Mapped[str | None] = mapped_column(String(64))
    related_entity_id: Mapped[UUID | None]
    failure_class: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str] = mapped_column(String(64))
    retryable: Mapped[bool]
    attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_attempts.id", ondelete="SET NULL")
    )
    trace_id: Mapped[str | None] = mapped_column(String(32))
    event_version: Mapped[str] = mapped_column(String(32), default="pipeline.failure.v1")
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    projected_status: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
