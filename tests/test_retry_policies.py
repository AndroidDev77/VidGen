"""Temporal retry policies for paid provider activities.

Every retry of a provider activity re-runs the whole stage against a paid
model, so the attempt cap is a budget control rather than a resilience knob.
"""

from __future__ import annotations

from packages.workflows.retry_policies import (
    NON_RETRYABLE_ERROR_TYPES,
    default_activity_retry_policy,
    provider_activity_retry_policy,
)


def test_provider_activities_stop_after_three_attempts() -> None:
    policy = provider_activity_retry_policy()
    assert policy.maximum_attempts == 3


def test_provider_activities_retry_less_than_ordinary_activities() -> None:
    assert (
        provider_activity_retry_policy().maximum_attempts
        < default_activity_retry_policy().maximum_attempts
    )


def test_provider_activities_never_retry_terminal_failures() -> None:
    non_retryable = set(provider_activity_retry_policy().non_retryable_error_types or ())
    assert set(NON_RETRYABLE_ERROR_TYPES) <= non_retryable
    # An exhausted provider quota does not recover within an activity's retry window.
    assert "QuotaError" in non_retryable
