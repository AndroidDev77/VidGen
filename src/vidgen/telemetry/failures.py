from __future__ import annotations

import asyncio
from typing import Any

from vidgen.contracts.telemetry import FailureClass, FailureClassification
from vidgen.telemetry.redaction import redact


def classify_failure(
    exc: BaseException,
    *,
    status_code: int | None = None,
    provider_error_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FailureClassification:
    name = type(exc).__name__.lower()
    if "unknownprovideroutcome" in name or "ambiguousvideosubmission" in name:
        kind, code, retry = FailureClass.UNKNOWN, "PROVIDER_OUTCOME_UNKNOWN", False
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        kind, code, retry = FailureClass.TIMEOUT, "PROVIDER_TIMEOUT", True
    elif isinstance(exc, asyncio.CancelledError):
        kind, code, retry = FailureClass.CANCELLED, "CANCELLED", False
    elif status_code == 429:
        kind, code, retry = FailureClass.RATE_LIMIT, "PROVIDER_RATE_LIMIT", True
    elif status_code in (401,):
        kind, code, retry = FailureClass.AUTHENTICATION, "PROVIDER_AUTHENTICATION", False
    elif status_code in (403,):
        kind, code, retry = FailureClass.AUTHORIZATION, "PROVIDER_AUTHORIZATION", False
    elif status_code is not None and 400 <= status_code < 500:
        kind, code, retry = FailureClass.INVALID_REQUEST, "PROVIDER_INVALID_REQUEST", False
    elif status_code is not None and status_code >= 500:
        kind, code, retry = FailureClass.TRANSPORT, "PROVIDER_UNAVAILABLE", True
    elif "connection" in name:
        kind, code, retry = FailureClass.TRANSPORT, "PROVIDER_TRANSPORT", True
    else:
        kind, code, retry = FailureClass.UNKNOWN, "PROVIDER_UNKNOWN", False
    return FailureClassification(
        failure_class=kind,
        error_code=code,
        retryable=retry,
        provider_http_status=status_code,
        provider_error_type=provider_error_type,
        sanitized_message=f"{type(exc).__name__} ({code})",
        diagnostic_metadata=redact(metadata or {}),
    )
