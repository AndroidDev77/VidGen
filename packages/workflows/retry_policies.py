from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

NON_RETRYABLE_ERROR_TYPES = ["PermanentError", "ValidationError", "CancelledError"]


def default_activity_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2,
        maximum_interval=timedelta(minutes=2),
        maximum_attempts=8,
        non_retryable_error_types=NON_RETRYABLE_ERROR_TYPES,
    )


def provider_activity_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2,
        maximum_interval=timedelta(minutes=5),
        maximum_attempts=12,
        non_retryable_error_types=[*NON_RETRYABLE_ERROR_TYPES, "QuotaError"],
    )
