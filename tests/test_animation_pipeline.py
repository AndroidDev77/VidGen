from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select

from packages.providers.image_generation import DeterministicFakeImageProvider
from services.animation.fake_provider import FakeVideoProvider
from services.animation.pipeline import AnimationPipeline
from services.image_generation.pipeline import ImageGenerationPipeline
from tests.storyboard_fixtures import build_fixture
from tests.test_storyboard_pipeline import run_pipeline as run_storyboard
from vidgen.db.animation_models import AnimationGeneratedVideo, RunwayTask
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.models import Asset
from vidgen.db.storyboard_models import StoryboardShotRecord


def prepared(tmp_path: Path):
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(project_id=fixture.project.id, idempotency_key="t14-for-animation")
    )
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    # The generic T13 fixture uses a 3.5-second visual-provider profile. T15's
    # Runway capability fixture requires an exact supported four-second job.
    shot.requested_generation_duration_us = 4_000_000
    shot.trim_end_us = 4_000_000 - shot.usable_duration_us
    shot.contract = {
        **shot.contract,
        "requested_generation_duration_us": 4_000_000,
        "trim_end_us": shot.trim_end_us,
    }
    fixture.session.commit()
    return fixture, shot


def test_fake_pipeline_persists_original_canonical_and_reuses(tmp_path: Path) -> None:
    fixture, shot = prepared(tmp_path)
    provider = FakeVideoProvider()
    pipeline = AnimationPipeline(
        fixture.session,
        fixture.blobs,
        provider,
        max_polls=2,
        poll_interval_seconds=0,
    )
    first = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t15-stable",
            shot_id=shot.id,
        )
    )
    assert first.status == "animation_complete"
    assert first.completed_count == 1
    assert first.items[0].shot_id == shot.stable_shot_id
    candidate = first.items[0].candidate
    assert candidate is not None
    assert candidate.original_asset_id != candidate.canonical_asset_id
    assert candidate.validation.valid
    attempts = fixture.session.scalar(
        select(func.count())
        .select_from(ProviderAttempt)
        .where(ProviderAttempt.operation == "video_generation")
    )
    assets = fixture.session.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.kind.in_(("runway_original_video", "canonical_shot_video")))
    )
    second = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t15-stable",
            shot_id=shot.id,
        )
    )
    assert second.reused_count == 1
    assert provider.submissions == 1
    assert fixture.session.scalar(select(func.count()).select_from(RunwayTask)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(AnimationGeneratedVideo)) == 1
    assert (
        fixture.session.scalar(
            select(func.count())
            .select_from(ProviderAttempt)
            .where(ProviderAttempt.operation == "video_generation")
        )
        == attempts
    )
    assert (
        fixture.session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(Asset.kind.in_(("runway_original_video", "canonical_shot_video")))
        )
        == assets
    )


def test_invalid_fake_dimensions_are_rejected_without_resubmission(tmp_path: Path) -> None:
    fixture, shot = prepared(tmp_path)
    provider = FakeVideoProvider(wrong_dimensions=True)
    pipeline = AnimationPipeline(
        fixture.session,
        fixture.blobs,
        provider,
        max_polls=2,
        poll_interval_seconds=0,
    )
    with pytest.raises(ValueError, match="technical validation"):
        asyncio.run(
            pipeline.process(
                project_id=fixture.project.id,
                idempotency_key="bad-dimensions",
                shot_id=shot.id,
            )
        )
    with pytest.raises(ValueError, match="technical validation"):
        asyncio.run(
            pipeline.process(
                project_id=fixture.project.id,
                idempotency_key="bad-dimensions",
                shot_id=shot.id,
            )
        )
    assert provider.submissions == 1


class AccountedFakeRunway(FakeVideoProvider):
    name = "runway"


def test_t23_reservation_reconciliation_is_idempotent(tmp_path: Path) -> None:
    from decimal import Decimal

    from vidgen.db.cost_models import CostLedgerEntry, CostReservation, ProjectBudget

    fixture, shot = prepared(tmp_path)
    fixture.session.add(
        ProjectBudget(
            project_id=fixture.project.id,
            warning_cap=Decimal("5"),
            hard_cap=Decimal("10"),
            currency="USD",
            policy_version="test",
        )
    )
    fixture.session.commit()
    provider = AccountedFakeRunway()
    pipeline = AnimationPipeline(
        fixture.session,
        fixture.blobs,
        provider,
        max_polls=2,
        poll_interval_seconds=0,
    )
    first = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="accounted",
            shot_id=shot.id,
        )
    )
    assert first.completed_count == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostReservation)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostLedgerEntry)) == 1
    budget = fixture.session.scalar(select(ProjectBudget))
    assert budget is not None
    assert budget.committed_amount == Decimal("0.200000")
    asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="accounted",
            shot_id=shot.id,
        )
    )
    assert fixture.session.scalar(select(func.count()).select_from(CostReservation)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostLedgerEntry)) == 1
