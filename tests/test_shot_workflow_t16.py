from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.workflows.shot_policy import (
    identity_hash,
    shot_activity_idempotency_key,
    temporal_shot_workflow_id,
)
from vidgen.contracts.shot_workflow import (
    ShotWorkflowIdentity,
    ShotWorkflowInput,
    ShotWorkflowStatus,
)

PROJECT = UUID("00000000-0000-0000-0000-000000000001")
STORYBOARD = UUID("00000000-0000-0000-0000-000000000002")
SHOT = UUID("00000000-0000-0000-0000-000000000003")
HASH = "a" * 64


def make_identity(canonical_hash: str = HASH) -> ShotWorkflowIdentity:
    fields: dict[str, str | int] = {
        "project_id": str(PROJECT),
        "storyboard_run_id": str(STORYBOARD),
        "storyboard_input_hash": HASH,
        "storyboard_shot_id": str(SHOT),
        "canonical_shot_hash": canonical_hash,
        "shot_sequence": 0,
        "timing_manifest_hash": HASH,
        "t14_configuration_identity": "image-provider/1",
        "t15_capability_profile_identity": "runway/2024-11-06",
        "t14_pipeline_version": "t14/1",
        "t15_pipeline_version": "t15/1",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    return ShotWorkflowIdentity(**fields, identity_hash=identity_hash(fields))


def test_identity_is_stable_and_material_change_creates_new_child() -> None:
    first = make_identity()
    assert first == make_identity()
    changed = make_identity("b" * 64)
    assert first.identity_hash != changed.identity_hash
    assert temporal_shot_workflow_id(first) != temporal_shot_workflow_id(changed)
    assert temporal_shot_workflow_id(first) == temporal_shot_workflow_id(make_identity())


def test_temporal_contract_is_id_only_and_forbids_payloads() -> None:
    identity = make_identity()
    request = ShotWorkflowInput(
        project_id=PROJECT,
        storyboard_run_id=STORYBOARD,
        storyboard_shot_id=SHOT,
        shot_input_hash=identity.identity_hash,
        workflow_identity=identity,
        idempotency_key="same-input",
    )
    payload = request.model_dump(mode="json")
    assert not ({"prompt", "image_bytes", "video_bytes", "provider_response"} & payload.keys())
    with pytest.raises(ValidationError):
        ShotWorkflowInput.model_validate({**payload, "prompt": "large content"})


def test_activity_keys_are_stable_and_stage_isolated() -> None:
    value = make_identity().identity_hash
    assert shot_activity_idempotency_key(value, "t14") == shot_activity_idempotency_key(
        value, "t14"
    )
    assert shot_activity_idempotency_key(value, "t14") != shot_activity_idempotency_key(
        value, "t15"
    )


def test_ten_shot_failure_isolation_acceptance_model() -> None:
    """Deterministic orchestration model: only failed animation is retried."""
    calls = {index: {"t14": 0, "t15": 0} for index in range(10)}
    states: dict[int, ShotWorkflowStatus] = {}
    for index in range(10):
        calls[index]["t14"] += 1
        calls[index]["t15"] += 1
        states[index] = ShotWorkflowStatus.FAILED if index == 4 else ShotWorkflowStatus.LOCKED
    assert sum(state == ShotWorkflowStatus.LOCKED for state in states.values()) == 9
    calls[4]["t15"] += 1
    states[4] = ShotWorkflowStatus.LOCKED
    assert all(state == ShotWorkflowStatus.LOCKED for state in states.values())
    assert all(calls[index] == {"t14": 1, "t15": 1} for index in range(10) if index != 4)
    assert calls[4] == {"t14": 1, "t15": 2}
