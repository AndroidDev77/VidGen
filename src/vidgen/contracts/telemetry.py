"""Versioned, secret-free observability contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field

from vidgen.contracts.common import StrictContract


class FrozenContract(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "1.0"


class FailureClass(StrEnum):
    TRANSPORT = "TRANSPORT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    CONTENT_FILTER = "CONTENT_FILTER"
    CONTRACT_VALIDATION = "CONTRACT_VALIDATION"
    QUALITY_FAILURE = "QUALITY_FAILURE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    DATABASE = "DATABASE"
    STORAGE = "STORAGE"
    TEMPORAL = "TEMPORAL"
    INTERNAL = "INTERNAL"
    UNKNOWN = "UNKNOWN"


class UsageUnit(StrEnum):
    INPUT_TOKEN = "INPUT_TOKEN"
    OUTPUT_TOKEN = "OUTPUT_TOKEN"
    CACHED_INPUT_TOKEN = "CACHED_INPUT_TOKEN"
    AUDIO_INPUT_SECOND = "AUDIO_INPUT_SECOND"
    AUDIO_OUTPUT_SECOND = "AUDIO_OUTPUT_SECOND"
    IMAGE_INPUT = "IMAGE_INPUT"
    IMAGE_OUTPUT = "IMAGE_OUTPUT"
    VIDEO_OUTPUT_SECOND = "VIDEO_OUTPUT_SECOND"
    REQUEST = "REQUEST"
    STORAGE_BYTE = "STORAGE_BYTE"
    COMPUTE_SECOND = "COMPUTE_SECOND"


class TraceContext(FrozenContract):
    traceparent: str | None = None
    tracestate: str | None = None


class TelemetryContext(FrozenContract):
    project_id: UUID | None = None
    related_entity_id: UUID | None = None
    temporal_workflow_id: str | None = None
    temporal_run_id: str | None = None
    trace: TraceContext = TraceContext()


class FailureClassification(FrozenContract):
    failure_class: FailureClass
    error_code: str
    retryable: bool
    provider_http_status: int | None = None
    provider_error_type: str | None = None
    sanitized_message: str
    diagnostic_metadata: dict[str, Any] = Field(default_factory=dict)


class UsageQuantity(FrozenContract):
    unit: UsageUnit
    quantity: Decimal = Field(ge=0)
    provider_reported: bool = False
    estimation_method: str | None = None
    source_field: str | None = None
    warnings: tuple[str, ...] = ()


class ProviderAttemptStarted(FrozenContract):
    attempt_id: UUID
    project_id: UUID
    related_entity_id: UUID | None = None
    provider: str
    model: str
    operation: str
    attempt_number: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str
    started_at: datetime


class ProviderAttemptCompleted(FrozenContract):
    attempt_id: UUID
    completed_at: datetime
    latency_ms: int = Field(ge=0)
    usage: tuple[UsageQuantity, ...] = ()
    provider_request_id: str | None = None
    actual_cost: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = "USD"
    warnings: tuple[str, ...] = ()


class ProviderAttemptFailed(FrozenContract):
    attempt_id: UUID
    completed_at: datetime
    latency_ms: int = Field(ge=0)
    failure: FailureClassification
    usage: tuple[UsageQuantity, ...] = ()


class ProviderAttemptRecord(ProviderAttemptStarted):
    status: str
    completed_at: datetime | None = None
    latency_ms: int | None = Field(None, ge=0)
    provider_request_id: str | None = None
    usage: tuple[UsageQuantity, ...] = ()
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    actual_cost: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = "USD"
    pricing_version_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    failure: FailureClassification | None = None
    redacted_result_reference: str | None = None
    warnings: tuple[str, ...] = ()


class PipelineFailureEvent(FrozenContract):
    event_id: UUID
    event_type: str = "pipeline.failure.v1"
    project_id: UUID
    workflow_id: str
    stage: str
    error_code: str
    failure_class: FailureClass
    retryable: bool
    projected_status: str
    created_at: datetime
    resolved_at: datetime | None = None


class ProviderMetricsSnapshot(FrozenContract):
    requests: int = Field(ge=0)
    failures: int = Field(ge=0)
    active: int = Field(ge=0)


class OperationsDashboardSummary(FrozenContract):
    failed_work: int = Field(ge=0)
    active_provider_attempts: int = Field(ge=0)
