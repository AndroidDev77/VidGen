from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select

from packages.providers.image_generation import DeterministicFakeImageProvider
from services.image_generation.openai_image import UnknownProviderOutcome
from services.image_generation.pipeline import (
    ImageGenerationPipeline,
    ProviderResponseRequiresReview,
)
from tests.storyboard_fixtures import build_fixture
from tests.test_storyboard_pipeline import run_pipeline as run_storyboard
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.image_generation_models import GeneratedKeyframeImage, ImageGenerationItem
from vidgen.db.models import Asset, Character
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
    shots = (
        fixture.session.execute(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
            .order_by(StoryboardShotRecord.global_sequence)
        )
        .scalars()
        .all()
    )
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


def test_a_regeneration_sequence_produces_a_new_keyframe_rather_than_the_old_one(
    tmp_path: Path,
) -> None:
    """A regeneration an owner paid to request must not return the same image.

    Nothing about an unchanged shot's material moves when a person asks for a
    different keyframe, so the identity resolves to the item that already
    exists, the run completes owning nothing, and the owner gets back the very
    image they rejected. Binding the shot workflow's regeneration sequence is
    what makes the request mean something.
    """
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    provider = DeterministicFakeImageProvider()
    pipeline = ImageGenerationPipeline(fixture.session, fixture.blobs, provider)
    original = asyncio.run(
        pipeline.process(project_id=fixture.project.id, idempotency_key="t14-original")
    )
    assert original.status == "keyframes_complete"

    # Sequence zero is the child T16 created, and is omitted from the hashed
    # material: every identity minted before the sequence existed keeps its hash.
    reused = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t14-same-material",
            shot_id=shot.id,
        )
    )
    assert reused.reused_count == 1
    assert reused.completed_count == 0

    regenerated = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t14-regenerated",
            shot_id=shot.id,
            regeneration_sequence=1,
        )
    )
    assert regenerated.status == "keyframes_complete"
    assert regenerated.completed_count == 1, "a regeneration generates rather than reuses"
    assert regenerated.reused_count == 0
    run_id = regenerated.run_id
    owned = fixture.session.scalars(
        select(ImageGenerationItem).where(ImageGenerationItem.run_id == run_id)
    ).all()
    assert [item.shot_id for item in owned] == [shot.id], "the run owns the item it generated"
    # The new candidate replaces the selection without overwriting the old one,
    # so the regenerated image is what T20 and T15 now see.
    frames = fixture.session.scalars(
        select(GeneratedKeyframeImage).where(
            GeneratedKeyframeImage.shot_id == shot.id,
            GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
        )
    ).all()
    assert len(frames) == 2
    selected = [frame for frame in frames if frame.selected]
    assert [frame.item_id for frame in selected] == [owned[0].id]

    # Replaying the same sequence is still idempotent: it reuses, never repays.
    replayed = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t14-regenerated-replay",
            shot_id=shot.id,
            regeneration_sequence=1,
        )
    )
    assert replayed.reused_count == 1
    assert replayed.completed_count == 0


def test_an_existing_run_keeps_the_sequence_it_was_created_with(tmp_path: Path) -> None:
    """The durable run row binds its own material, not whatever a caller passes.

    A replacement child's T14 activity can be re-entered after a deploy that
    changed what the sequence binds. Recomputing from the argument would refuse
    the run's own idempotency key as binding different material - and, if it got
    past that, would try to write a second item into the same (run, shot, role)
    slot. Reading the sequence back off the run makes re-entry resolve exactly
    the identities that run already wrote.
    """
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    pipeline = ImageGenerationPipeline(
        fixture.session, fixture.blobs, DeterministicFakeImageProvider()
    )
    original = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t14-in-flight",
            shot_id=shot.id,
        )
    )
    assert original.completed_count == 1

    # The same run, re-entered by a worker that now binds a sequence.
    resumed = asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="t14-in-flight",
            shot_id=shot.id,
            regeneration_sequence=4,
        )
    )
    assert resumed.run_id == original.run_id
    assert resumed.reused_count == 1, "it resolves the item this run already wrote"
    assert resumed.completed_count == 0
    owned = fixture.session.scalar(
        select(func.count())
        .select_from(ImageGenerationItem)
        .where(ImageGenerationItem.run_id == original.run_id)
    )
    assert owned == 1, "and never writes a second item into the same slot"


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
    assert "Location 0" in prompt
    assert str(shot.contract["incoming_continuity"]["present_character_ids"][0]) not in prompt


def test_prompt_resolves_offscreen_prop_owner(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    storyboard = run_storyboard(fixture)
    shot = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(StoryboardShotRecord.storyboard_run_id == storyboard.storyboard_run_id)
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert shot is not None
    owner = Character(
        project_id=fixture.project.id,
        canonical_name="Offscreen Owner",
        definition={"canonical_name": "Offscreen Owner", "aliases": ["The Courier"]},
    )
    fixture.session.add(owner)
    fixture.session.flush()
    continuity = dict(shot.contract["incoming_continuity"])
    continuity["props"] = [
        {
            "schema_version": "1.0",
            "prop_id": "sealed-envelope",
            "owner_character_id": str(owner.id),
            "note": "held by the visible subject",
        }
    ]
    shot.contract = {**shot.contract, "incoming_continuity": continuity}
    fixture.session.commit()
    asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(
            project_id=fixture.project.id,
            idempotency_key="offscreen-owner",
            shot_id=shot.id,
        )
    )
    item = fixture.session.scalar(select(ImageGenerationItem))
    assert item is not None
    assert "sealed-envelope owned by Offscreen Owner" in item.prompt_package["prompt"]
