"""End-to-end routing through the T15 pipeline, budgets, identities and Temporal messages.

The deterministic fake provider renders real MP4s with FFmpeg; a subclass named
``runway`` exercises the budget path. No paid provider is ever called.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from packages.workflows.shot_policy import identity_hash
from services.animation.pipeline import AnimationPipeline
from services.animation.routing import BudgetDenied
from services.generation.settings import with_generation_settings
from tests.test_animation_pipeline import AccountedFakeRunway, prepared
from vidgen.contracts.animation import RunwayModel
from vidgen.contracts.generation import (
    GenerationQuality,
    ProjectGenerationSettings,
    RoutingDecision,
    RoutingReasonCode,
)
from vidgen.contracts.shot_workflow import ProjectShotFanoutInput, ShotWorkflowIdentity
from vidgen.db.animation_models import AnimationItem, RunwayTask
from vidgen.db.cost_models import CostLedgerEntry, CostReservation, ProjectBudget, ProviderAttempt
from vidgen.db.models import Asset
from vidgen.db.storyboard_models import StoryboardShotRecord


def _configure(fixture, quality: GenerationQuality, **extra) -> None:
    fixture.project.settings = with_generation_settings(
        fixture.project.settings,
        ProjectGenerationSettings(generation_quality=quality, **extra),
    )
    fixture.session.commit()


def _budget(fixture, hard_cap: str) -> ProjectBudget:
    budget = ProjectBudget(
        project_id=fixture.project.id,
        warning_cap=Decimal("0"),
        hard_cap=Decimal(hard_cap),
        currency="USD",
        policy_version="test",
    )
    fixture.session.add(budget)
    fixture.session.commit()
    return budget


def _pipeline(fixture, provider, **kwargs) -> AnimationPipeline:
    return AnimationPipeline(
        fixture.session, fixture.blobs, provider, max_polls=2, poll_interval_seconds=0, **kwargs
    )


def _run(pipeline, fixture, shot, key: str):
    return asyncio.run(
        pipeline.process(project_id=fixture.project.id, idempotency_key=key, shot_id=shot.id)
    )


def _item(fixture, shot) -> AnimationItem:
    item = fixture.session.scalar(select(AnimationItem).where(AnimationItem.shot_id == shot.id))
    assert item is not None
    return item


def test_premium_animates_the_shot_with_gen4_5_and_records_why(tmp_path: Path) -> None:
    fixture, shot = prepared(tmp_path)
    _configure(fixture, GenerationQuality.PREMIUM)
    _budget(fixture, "10")
    provider = AccountedFakeRunway()
    result = _run(_pipeline(fixture, provider), fixture, shot, "premium")
    assert result.completed_count == 1
    item = _item(fixture, shot)
    assert item.model == RunwayModel.GEN4_5.value
    decision = RoutingDecision.model_validate(item.routing_decision)
    assert decision.reason_code is RoutingReasonCode.PREMIUM_GEN4_5
    assert decision.quality_mode is GenerationQuality.PREMIUM
    assert decision.generation_duration_seconds == 7
    # The attempt, the task projection and the asset all name the real model.
    task = fixture.session.scalar(select(RunwayTask))
    assert task is not None
    assert task.request_projection["model"] == "gen4.5"
    assert task.request_projection["routing"]["routing_reason"] == "premium_gen4_5"
    attempt = fixture.session.get(ProviderAttempt, task.provider_attempt_id)
    assert attempt is not None
    assert attempt.model == "gen4.5"
    assert attempt.redacted_metadata["routing_reason"] == "premium_gen4_5"
    assert attempt.usage == [{"unit": "VIDEO_OUTPUT_SECOND", "quantity": 7}]
    assert attempt.actual_cost == Decimal("0.840000")
    original = fixture.session.scalar(select(Asset).where(Asset.kind == "runway_original_video"))
    assert original is not None
    assert original.generation_parameters["model"] == "gen4.5"
    assert original.generation_parameters["routing_reason"] == "premium_gen4_5"
    budget = fixture.session.scalar(select(ProjectBudget))
    assert budget is not None and budget.committed_amount == Decimal("0.840000")
    assert budget.reserved_amount == Decimal("0")


def test_premium_is_refused_before_any_submission_when_the_budget_cannot_cover_it(
    tmp_path: Path,
) -> None:
    fixture, shot = prepared(tmp_path)
    _configure(fixture, GenerationQuality.PREMIUM)
    _budget(fixture, "0.50")
    provider = AccountedFakeRunway()
    with pytest.raises(BudgetDenied, match="premium_fallback_allowed"):
        _run(_pipeline(fixture, provider), fixture, shot, "premium-denied")
    assert provider.submissions == 0
    assert fixture.session.scalar(select(func.count()).select_from(CostReservation)) == 0
    assert fixture.session.scalar(select(func.count()).select_from(RunwayTask)) == 0
    assert fixture.session.scalar(select(func.count()).select_from(AnimationItem)) == 0


def test_premium_falls_back_to_turbo_only_when_configured(tmp_path: Path) -> None:
    fixture, shot = prepared(tmp_path)
    _configure(fixture, GenerationQuality.PREMIUM, premium_fallback_allowed=True)
    _budget(fixture, "0.50")
    result = _run(_pipeline(fixture, AccountedFakeRunway()), fixture, shot, "premium-fallback")
    assert result.completed_count == 1
    item = _item(fixture, shot)
    assert item.model == RunwayModel.GEN4_TURBO.value
    assert item.routing_decision["reason_code"] == "premium_fallback_budget"


def test_balanced_upgrades_the_hero_shot_and_keeps_turbo_for_the_rest(tmp_path: Path) -> None:
    fixture, hero = prepared(tmp_path)
    assert hero.contract["provenance"]["hero_shot"] is True
    _configure(fixture, GenerationQuality.BALANCED)
    _budget(fixture, "10")
    provider = AccountedFakeRunway()
    pipeline = _pipeline(fixture, provider)
    result = asyncio.run(
        pipeline.process(project_id=fixture.project.id, idempotency_key="balanced-all")
    )
    assert result.failed_count == 0
    items = fixture.session.scalars(
        select(AnimationItem).order_by(AnimationItem.shot_sequence)
    ).all()
    assert items[0].model == "gen4.5"
    assert items[0].routing_decision["reason_code"] == "balanced_hero_premium"
    assert all(item.model == "gen4_turbo" for item in items[1:])
    assert all(
        item.routing_decision["reason_code"] == "balanced_default_turbo" for item in items[1:]
    )


def test_economy_never_upgrades_the_hero_shot(tmp_path: Path) -> None:
    fixture, hero = prepared(tmp_path)
    _configure(fixture, GenerationQuality.ECONOMY)
    _budget(fixture, "10")
    _run(_pipeline(fixture, AccountedFakeRunway()), fixture, hero, "economy")
    item = _item(fixture, hero)
    assert item.model == "gen4_turbo"
    assert item.routing_decision["reason_code"] == "economy_turbo"
    assert item.routing_decision["hero_shot"] is True


def test_an_explicit_premium_request_is_honoured_in_premium_mode(tmp_path: Path) -> None:
    fixture, shot = prepared(tmp_path)
    _configure(fixture, GenerationQuality.PREMIUM)
    result = _run(
        _pipeline(fixture, AccountedFakeRunway(), requested_model=RunwayModel.GEN4_5),
        fixture,
        shot,
        "explicit",
    )
    assert result.completed_count == 1
    assert _item(fixture, shot).routing_decision["reason_code"] == "explicit_model"


def test_a_completed_compatible_attempt_is_reused_and_never_charged_twice(
    tmp_path: Path,
) -> None:
    fixture, hero = prepared(tmp_path)
    # An ordinary (non-hero) shot: the second segment's opening shot.
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(
            StoryboardShotRecord.storyboard_run_id == hero.storyboard_run_id,
            StoryboardShotRecord.global_sequence > hero.global_sequence,
        )
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    assert shot.contract["provenance"]["hero_shot"] is False
    _configure(fixture, GenerationQuality.ECONOMY)
    _budget(fixture, "10")
    provider = AccountedFakeRunway()
    first = _run(_pipeline(fixture, provider), fixture, shot, "economy-run")
    assert first.completed_count == 1
    # A balanced project still routes this ordinary shot to Gen-4 Turbo, so the
    # completed clip is exactly the output the new run would produce: it is
    # reused, with no new submission, reservation, ledger entry or asset.
    _configure(fixture, GenerationQuality.BALANCED)
    before_assets = fixture.session.scalar(select(func.count()).select_from(Asset))
    second = _run(_pipeline(fixture, provider), fixture, shot, "balanced-run")
    assert second.reused_count == 1
    assert provider.submissions == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostReservation)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostLedgerEntry)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(Asset)) == before_assets
    # The very same run replayed is free as well.
    third = _run(_pipeline(fixture, provider), fixture, shot, "balanced-run")
    assert third.reused_count == 1
    video_attempts = (
        select(func.count())
        .select_from(ProviderAttempt)
        .where(ProviderAttempt.operation == "video_generation")
    )
    assert fixture.session.scalar(video_attempts) == 1


def test_changing_the_quality_mode_never_reuses_an_incompatible_clip(tmp_path: Path) -> None:
    fixture, hero = prepared(tmp_path)
    _configure(fixture, GenerationQuality.ECONOMY)
    _budget(fixture, "10")
    provider = AccountedFakeRunway()
    _run(_pipeline(fixture, provider), fixture, hero, "economy-hero")
    _configure(fixture, GenerationQuality.PREMIUM)
    # The same idempotency key now binds different material: refused outright.
    with pytest.raises(ValueError, match="different material"):
        _run(_pipeline(fixture, provider), fixture, hero, "economy-hero")
    # A new run under premium generates a Gen-4.5 clip; the Turbo clip stays as history.
    result = _run(_pipeline(fixture, provider), fixture, hero, "premium-hero")
    assert result.completed_count == 1
    assert provider.submissions == 2
    models = sorted(item.model for item in fixture.session.scalars(select(AnimationItem)).all())
    assert models == ["gen4.5", "gen4_turbo"]
    assert fixture.session.scalar(select(func.count()).select_from(CostLedgerEntry)) == 2


def test_temporal_messages_stay_compact_and_bind_the_generation_policy() -> None:
    fields: dict[str, str | int] = {
        "project_id": str(UUID(int=1)),
        "storyboard_run_id": str(UUID(int=2)),
        "storyboard_input_hash": "a" * 64,
        "storyboard_shot_id": str(UUID(int=3)),
        "canonical_shot_hash": "b" * 64,
        "shot_sequence": 0,
        "timing_manifest_hash": "c" * 64,
        "t14_configuration_identity": "image-provider/1",
        "t15_capability_profile_identity": "runway/2024-11-06",
        "t14_pipeline_version": "t14/1",
        "t15_pipeline_version": "t15/1",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    legacy = ShotWorkflowIdentity(**fields, identity_hash=identity_hash(fields))
    # An identity minted before the policy existed keeps its hash.
    assert legacy.generation_policy_identity == ""
    assert legacy.material() == fields
    policy = "gq=balanced;sp=normal;pf=0;rp=runway-routing-v2;qr=quality-repair/1;cp=x@1;rg=2"
    bound_fields = {**fields, "generation_policy_identity": policy}
    bound = ShotWorkflowIdentity(
        **fields, generation_policy_identity=policy, identity_hash=identity_hash(bound_fields)
    )
    assert bound.identity_hash != legacy.identity_hash
    changed = bound_fields | {"generation_policy_identity": policy.replace("balanced", "premium")}
    assert identity_hash(changed) != bound.identity_hash
    with pytest.raises(ValidationError):
        ShotWorkflowIdentity(
            **fields, generation_policy_identity=policy, identity_hash=legacy.identity_hash
        )
    # Only the compact identifier crosses into history: never a profile, a
    # prompt or a payload, and never anything over the bounded length.
    fanout = ProjectShotFanoutInput(
        project_id=UUID(int=1),
        storyboard_run_id=UUID(int=2),
        idempotency_key="t16",
        generation_policy_identity=policy,
    )
    assert fanout.generation_policy_identity == policy
    with pytest.raises(ValidationError):
        ProjectShotFanoutInput(
            project_id=UUID(int=1),
            storyboard_run_id=UUID(int=2),
            idempotency_key="t16",
            generation_policy_identity="x" * 161,
        )
    with pytest.raises(ValidationError):
        ProjectShotFanoutInput.model_validate(
            {**fanout.model_dump(mode="json"), "capability_profile": {"durations": [2, 3]}}
        )


def test_routing_refusals_reach_temporal_typed_actionable_and_non_retryable() -> None:
    from temporalio.exceptions import ApplicationError

    from services.animation.routing import UnsupportedCapability
    from workers.temporal_worker.production_handlers import terminal_animation_error

    denied = terminal_animation_error(
        BudgetDenied("premium mode cannot afford this shot: raise the hard cap")
    )
    assert isinstance(denied, ApplicationError)
    assert denied.non_retryable is True
    assert denied.type == "BudgetDenied"
    assert "raise the hard cap" in str(denied)
    unsupported = terminal_animation_error(UnsupportedCapability("Gen-4.5 is not available"))
    assert unsupported is not None and unsupported.type == "UnsupportedCapability"
    # A transport failure stays retryable, so no terminal error is raised for it.
    assert terminal_animation_error(ConnectionError("reset")) is None
    other = terminal_animation_error(RuntimeError("provider output failed validation"))
    assert other is not None and other.non_retryable is True and other.type == "RuntimeError"
