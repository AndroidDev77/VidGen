"""Replay-stable T16 policy and workflow identity helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from temporalio.common import RetryPolicy

from vidgen.contracts.shot_workflow import ShotWorkflowIdentity

TASK_QUEUE = "vidgen-projects"
DEFAULT_FANOUT_CONCURRENCY = 10
ACTIVITY_TIMEOUT = timedelta(hours=2)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)


def identity_hash(fields: dict[str, str | int]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def temporal_shot_workflow_id(identity: ShotWorkflowIdentity) -> str:
    return f"vidgen-shot-{identity.storyboard_shot_id}-{identity.identity_hash[:24]}"


def shot_activity_idempotency_key(identity_hash_value: str, operation: str) -> str:
    return f"t16:{identity_hash_value}:{operation}"


def shot_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2,
        maximum_interval=timedelta(minutes=2),
        maximum_attempts=6,
        non_retryable_error_types=[
            "InvalidLineage",
            "DeterministicConfigurationFailure",
            "BudgetDenied",
            "UnsupportedCapability",
            "UnknownProviderOutcome",
            "CancelledError",
        ],
    )
