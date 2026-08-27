"""Reconstruct the T16 child-workflow identity from persisted rows.

A shot command must be signalled to the *actual* Temporal child, whose ID binds
the full T16 material identity, not to an invented one. Every field of that
identity is derivable from data T13-T15 already persisted, so T18 rebuilds it
here with the same helpers the T16 fan-out activity uses rather than guessing a
workflow ID.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy.orm import Session

from packages.workflows.shot_policy import identity_hash, temporal_shot_workflow_id
from services.animation.pipeline import PIPELINE_VERSION as T15_PIPELINE_VERSION
from services.image_generation.pipeline import PIPELINE_VERSION as T14_PIPELINE_VERSION
from vidgen.contracts.shot_workflow import ShotWorkflowIdentity
from vidgen.db.models import Asset
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.review.errors import not_found


def configuration_identities(
    *,
    image_provider_name: str,
    image_model: str,
    video_provider_name: str,
    visual_capability_profile: str,
) -> tuple[str, str]:
    """Return the T14 and T15 identity strings the T16 fan-out binds into a child ID.

    These are opaque configuration labels, not provider calls; they are built
    here so the route layer never composes provider-specific strings.
    """
    return (
        f"{image_provider_name}:{image_model}:image-provider/1",
        f"{video_provider_name}:{visual_capability_profile}:runway/2024-11-06",
    )


def canonical_shot_hash(contract: object) -> str:
    """The same canonical contract hash the T16 fan-out activity computes."""
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def shot_workflow_identity(
    session: Session,
    run: StoryboardRun,
    shot: StoryboardShotRecord,
    *,
    t14_configuration_identity: str,
    t15_capability_profile_identity: str,
) -> ShotWorkflowIdentity:
    """Rebuild the current child identity for one shot of a selected storyboard."""
    if run.timing_manifest_asset_id is None:
        raise not_found("shot workflow")
    timing_asset = session.get(Asset, run.timing_manifest_asset_id)
    if timing_asset is None:
        raise not_found("shot workflow")
    material: dict[str, str | int] = {
        "project_id": str(run.project_id),
        "storyboard_run_id": str(run.id),
        "storyboard_input_hash": run.input_hash,
        "storyboard_shot_id": str(shot.stable_shot_id),
        "canonical_shot_hash": canonical_shot_hash(shot.contract),
        "shot_sequence": shot.global_sequence,
        "timing_manifest_hash": timing_asset.sha256,
        "t14_configuration_identity": t14_configuration_identity,
        "t15_capability_profile_identity": t15_capability_profile_identity,
        "t14_pipeline_version": T14_PIPELINE_VERSION,
        "t15_pipeline_version": T15_PIPELINE_VERSION,
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    # ``material`` is the exact, ordered field set the T16 hash binds; validate
    # it back through the contract so a drift in either side fails loudly.
    return ShotWorkflowIdentity.model_validate(
        {**material, "identity_hash": identity_hash(material)}
    )


def current_workflow_id(identity: ShotWorkflowIdentity) -> str:
    """The Temporal ID of the child that currently owns this shot."""
    return temporal_shot_workflow_id(identity)


def regenerated_workflow_id(stable_shot_id: UUID, new_identity_hash: str) -> str:
    """The Temporal ID the replacement child will take, in T16's own format."""
    return f"vidgen-shot-{stable_shot_id}-{new_identity_hash[:24]}"
