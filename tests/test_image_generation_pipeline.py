from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select

from services.image_generation.fake_provider import DeterministicFakeImageProvider
from services.image_generation.openai_image import UnknownProviderOutcome
from services.image_generation.pipeline import (
    ImageGenerationPipeline,
    ProviderResponseRequiresReview,
)
from tests.storyboard_fixtures import build_fixture
from tests.test_storyboard_pipeline import run_pipeline as run_storyboard
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.image_generation_models import GeneratedKeyframeImage, ImageGenerationItem
from vidgen.db.models import Asset
from vidgen.db.storyboard_models import StoryboardShotRecord


class AmbiguousProvider(DeterministicFakeImageProvider):
    async def generate(self, request, reference_bytes=()):  # type: ignore[no-untyped-def]
        self.call_count += 1
        raise UnknownProviderOutcome("accepted request may have timed out")


class UnbudgetedProductionProvider(DeterministicFakeImageProvider):
    name = "openai"


def test_pipeline_persists_and_reuses_every_required_first_frame(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    provider = DeterministicFakeImageProvider()
    pipeline = ImageGenerationPipeline(fixture.session, fixture.blobs, provider)
    first = asyncio.run(
        pipeline.process(project_id=fixture.project.id, idempotency_key="t14-stable")
    )
    assert first.status == "keyframes_complete"
    assert first.completed_count == storyboard.shot_count
    assert all(item.keyframe_role.value == "FIRST_FRAME" for item in first.items)
    asset_count = fixture.session.scalar(
        select(func.count()).select_from(Asset).where(Asset.kind == "generated_keyframe")
    )
    attempts = fixture.session.scalar(
        select(func.count())
        .select_from(ProviderAttempt)
        .where(ProviderAttempt.operation == "image_generation")
    )
    second = asyncio.run(
        pipeline.process(project_id=fixture.project.id, idempotency_key="t14-stable")
    )
    assert second.reused_count == storyboard.shot_count
    assert provider.call_count == storyboard.shot_count
    assert (
        fixture.session.scalar(select(func.count()).select_from(ImageGenerationItem))
        == storyboard.shot_count
    )
    assert (
        fixture.session.scalar(select(func.count()).select_from(GeneratedKeyframeImage))
        == storyboard.shot_count
    )
    assert (
        fixture.session.scalar(
            select(func.count()).select_from(Asset).where(Asset.kind == "generated_keyframe")
        )
        == asset_count
    )
    assert (
        fixture.session.scalar(
            select(func.count())
            .select_from(ProviderAttempt)
            .where(ProviderAttempt.operation == "image_generation")
        )
        == attempts
    )


def test_pipeline_rejects_stale_selected_storyboard(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    run_storyboard(fixture)
    fixture.script.selected = False
    fixture.session.commit()
    pipeline = ImageGenerationPipeline(
        fixture.session, fixture.blobs, DeterministicFakeImageProvider()
    )
    try:
        asyncio.run(pipeline.process(project_id=fixture.project.id, idempotency_key="stale"))
    except ValueError as exc:
        assert "script_unselected" in str(exc)
    else:
        raise AssertionError("stale lineage was accepted")


def test_run_idempotency_binds_shot_and_role_selectors(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shots = fixture.session.execute(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    ).scalars().all()
    pipeline = ImageGenerationPipeline(
        fixture.session, fixture.blobs, DeterministicFakeImageProvider()
    )
    asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="scoped",
            shot_id=shots[0].id,
        )
    )
    with pytest.raises(ValueError, match="different material inputs"):
        asyncio.run(
            pipeline.process(
                project_id=fixture.project.id,
                idempotency_key="scoped",
                shot_id=shots[1].id,
            )
        )


def test_explicit_last_frame_contract_is_generated(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    shot.contract = {**shot.contract, "requires_last_frame": True}
    fixture.session.commit()
    result = asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(project_id=fixture.project.id, idempotency_key="with-last")
    )
    assert result.requested_count == storyboard.shot_count + 1
    assert any(item.keyframe_role.value == "LAST_FRAME" for item in result.items)


def test_new_material_candidate_replaces_selection_without_overwrite(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    run_storyboard(fixture)
    first_pipeline = ImageGenerationPipeline(
        fixture.session, fixture.blobs, DeterministicFakeImageProvider(), quality="low"
    )
    asyncio.run(first_pipeline.process(project_id=fixture.project.id, idempotency_key="low"))
    second_pipeline = ImageGenerationPipeline(
        fixture.session, fixture.blobs, DeterministicFakeImageProvider(), quality="high"
    )
    asyncio.run(second_pipeline.process(project_id=fixture.project.id, idempotency_key="high"))
    total = fixture.session.scalar(select(func.count()).select_from(GeneratedKeyframeImage))
    selected = fixture.session.scalar(
        select(func.count())
        .select_from(GeneratedKeyframeImage)
        .where(GeneratedKeyframeImage.selected)
    )
    assert selected is not None
    assert total == 2 * selected


def test_ambiguous_outcome_is_durable_and_never_resubmitted(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    provider = AmbiguousProvider()
    pipeline = ImageGenerationPipeline(fixture.session, fixture.blobs, provider)
    for _ in range(2):
        with pytest.raises(UnknownProviderOutcome):
            asyncio.run(
                pipeline.process(
                    project_id=fixture.project.id,
                    idempotency_key="ambiguous",
                    shot_id=shot.id,
                )
            )
    assert provider.call_count == 1
    item = fixture.session.scalar(select(ImageGenerationItem))
    assert item is not None
    assert item.status == "provider_outcome_unknown"


def test_unbudgeted_project_can_use_configured_provider(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    result = asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, UnbudgetedProductionProvider()
        ).process(
            project_id=fixture.project.id,
            idempotency_key="unbudgeted-production",
            shot_id=shot.id,
        )
    )
    assert result.completed_count == 1


def test_known_provider_response_is_not_regenerated_after_validation_failure(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    provider = DeterministicFakeImageProvider(wrong_dimensions=True)
    pipeline = ImageGenerationPipeline(fixture.session, fixture.blobs, provider)
    for expected_error in (ValueError, ProviderResponseRequiresReview):
        with pytest.raises(expected_error):
            asyncio.run(
                pipeline.process(
                    project_id=fixture.project.id,
                    idempotency_key="invalid-known-response",
                    shot_id=shot.id,
                )
            )
    assert provider.call_count == 1


def test_results_and_prompts_use_canonical_t13_identities(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    result = asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(
            project_id=fixture.project.id,
            idempotency_key="canonical-identities",
            shot_id=shot.id,
        )
    )
    assert result.items[0].shot_id == shot.stable_shot_id
    item = fixture.session.scalar(select(ImageGenerationItem))
    assert item is not None
    prompt = item.prompt_package["prompt"]
    assert "Character 0" in prompt
    assert str(shot.contract["incoming_continuity"]["present_character_ids"][0]) not in prompt
