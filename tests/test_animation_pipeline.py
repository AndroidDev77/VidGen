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
from vidgen.db.animation_repository import AnimationLineageError, AnimationRepository
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
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
    # The storyboard plans against the Runway Gen-4 Turbo profile, so the shot
    # already carries a whole-second generation duration the adapter accepts.
    assert shot.requested_generation_duration_us % 1_000_000 == 0
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
    # 6.5 s of narration rounds up to a 7-second Gen-4 Turbo job at $0.05/s.
    assert budget.committed_amount == Decimal("0.350000")
    asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            idempotency_key="accounted",
            shot_id=shot.id,
        )
    )
    assert fixture.session.scalar(select(func.count()).select_from(CostReservation)) == 1
    assert fixture.session.scalar(select(func.count()).select_from(CostLedgerEntry)) == 1


def _shot_workflow_input(fixture, shot: StoryboardShotRecord, *, keyframe_asset_id=None):
    """A replacement child's input for one prepared fixture shot."""
    from packages.workflows.shot_policy import identity_hash
    from vidgen.contracts.shot_workflow import ShotWorkflowIdentity, ShotWorkflowInput

    fields: dict[str, str | int] = {
        "regeneration_sequence": 1,
        "project_id": str(fixture.project.id),
        "storyboard_run_id": str(shot.storyboard_run_id),
        "storyboard_input_hash": "a" * 64,
        "storyboard_shot_id": str(shot.stable_shot_id),
        "canonical_shot_hash": "b" * 64,
        "shot_sequence": shot.global_sequence,
        "timing_manifest_hash": "c" * 64,
        "t14_configuration_identity": "image-provider/1",
        "t15_capability_profile_identity": "runway/2024-11-06",
        "t14_pipeline_version": "t14/1",
        "t15_pipeline_version": "t15/1",
        "t16_workflow_version": "t16/1",
        "attempt_policy_version": "shot-attempt/1",
    }
    identity = ShotWorkflowIdentity(**fields, identity_hash=identity_hash(fields))
    return ShotWorkflowInput(
        project_id=fixture.project.id,
        storyboard_run_id=shot.storyboard_run_id,
        storyboard_shot_id=shot.stable_shot_id,
        shot_input_hash=identity.identity_hash,
        workflow_identity=identity,
        selected_keyframe_asset_id=keyframe_asset_id,
        idempotency_key="t18b-replacement",
    )


def _reuse_only_t14_run(fixture, shot: StoryboardShotRecord, idempotency_key: str):
    """The empty ``keyframes_complete`` run a pre-fix replacement child left behind.

    Runs exactly like the retry that caused this: a per-shot T14 under a brand
    new idempotency key, whose material identity is unchanged, so every item is
    reused from the run that first generated it and this run completes owning
    none of its own.
    """
    result = asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(
            project_id=fixture.project.id,
            idempotency_key=idempotency_key,
            shot_id=shot.id,
        )
    )
    assert result.status == "keyframes_complete"
    assert result.reused_count == result.requested_count
    owned = fixture.session.scalar(
        select(func.count())
        .select_from(ImageGenerationItem)
        .where(ImageGenerationItem.run_id == result.run_id)
    )
    assert owned == 0, "the run this bug is about is complete and owns nothing"
    return fixture.session.get(ImageGenerationRun, result.run_id)


def test_a_reuse_only_t14_run_never_becomes_a_shots_authority(tmp_path: Path) -> None:
    """The empty run must not displace the one holding the selected keyframe.

    Every retry of a shot with an existing keyframe created one of these, and
    they are already in deployed databases. Animation has to keep working
    alongside them without anybody cleaning them up.
    """
    fixture, shot = prepared(tmp_path)
    empty = _reuse_only_t14_run(fixture, shot, "t14-retry-child")
    assert empty is not None

    inputs = AnimationRepository(fixture.session).authoritative_inputs(
        fixture.project.id, shot_id=shot.stable_shot_id
    )

    assert inputs.image_run.id != empty.id
    first = inputs.keyframes[shot.id].first
    item = fixture.session.get(ImageGenerationItem, first.item_id)
    assert item is not None
    assert item.run_id == inputs.image_run.id, "authority is the run holding the keyframe"


def test_a_sibling_shots_newer_run_is_still_not_this_shots_authority(tmp_path: Path) -> None:
    """The protection the per-shot scoping was introduced for stays intact.

    A sibling's own T14 run is the project's newest, and it holds nothing of
    this shot's, so it must never be what this shot animates from.
    """
    fixture, shot = prepared(tmp_path)
    sibling = fixture.session.scalar(
        select(StoryboardShotRecord)
        .where(
            StoryboardShotRecord.storyboard_run_id == shot.storyboard_run_id,
            StoryboardShotRecord.id != shot.id,
        )
        .order_by(StoryboardShotRecord.global_sequence)
    )
    assert sibling is not None
    newer = asyncio.run(
        ImageGenerationPipeline(
            fixture.session, fixture.blobs, DeterministicFakeImageProvider()
        ).process(
            project_id=fixture.project.id,
            idempotency_key="t14-sibling-regeneration",
            shot_id=sibling.id,
            regeneration_sequence=1,
        )
    )
    assert newer.completed_count == 1, "the sibling genuinely generated a new keyframe"

    inputs = AnimationRepository(fixture.session).authoritative_inputs(
        fixture.project.id, shot_id=shot.stable_shot_id
    )

    assert inputs.image_run.id != newer.run_id
    # And naming the sibling's run for this shot is still refused outright.
    with pytest.raises(AnimationLineageError) as refused:
        AnimationRepository(fixture.session).authoritative_inputs(
            fixture.project.id, image_run_id=newer.run_id, shot_id=shot.stable_shot_id
        )
    assert refused.value.code == "image_run_stale"


def test_a_retrying_child_resolves_the_run_that_holds_its_keyframe(tmp_path: Path) -> None:
    """The whole chain that failed, from the child's own run to the lineage gate.

    A replacement child's T14 run reused every keyframe and owns no items, so
    it is not a run T15 may name: naming it is what raised ``image_run_stale``
    on all ten retries. The activity ignores its own empty run, resolves the
    run holding the shot's selected keyframe instead, and that run is the one
    the lineage gate accepts.
    """
    from packages.workflows.shot_policy import shot_activity_idempotency_key
    from workers.temporal_worker.production_handlers import (
        _keyframe_image_run,
        _own_t14_image_run,
    )

    fixture, shot = prepared(tmp_path)
    request = _shot_workflow_input(fixture, shot)
    empty = _reuse_only_t14_run(
        fixture, shot, shot_activity_idempotency_key(request.shot_input_hash, "t14")
    )
    assert empty is not None

    assert _own_t14_image_run(fixture.session, request, shot) is None
    resolved = _keyframe_image_run(fixture.session, request, shot)
    assert resolved is not None
    assert resolved.id != empty.id

    # Exactly the call T15 opens with, on exactly the run the activity picked.
    inputs = AnimationRepository(fixture.session).authoritative_inputs(
        fixture.project.id, image_run_id=resolved.id, shot_id=request.storyboard_shot_id
    )
    assert inputs.image_run.id == resolved.id
    assert shot.id in inputs.keyframes

    # Naming the empty run - what the activity used to do - is still refused.
    with pytest.raises(AnimationLineageError) as refused:
        AnimationRepository(fixture.session).authoritative_inputs(
            fixture.project.id, image_run_id=empty.id, shot_id=request.storyboard_shot_id
        )
    assert refused.value.code == "image_run_stale"


def test_a_child_handed_an_approved_keyframe_animates_that_runs_keyframe(tmp_path: Path) -> None:
    """#77 stays true: a carried keyframe still selects the run that made it.

    It also stays exclusive - a child handed one image must never resolve to a
    different one - so a keyframe that is no longer this shot's selection
    resolves to nothing rather than to whatever is.
    """
    from workers.temporal_worker.production_handlers import _keyframe_image_run

    fixture, shot = prepared(tmp_path)
    frame = fixture.session.scalar(
        select(GeneratedKeyframeImage).where(
            GeneratedKeyframeImage.shot_id == shot.id,
            GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
            GeneratedKeyframeImage.selected,
        )
    )
    assert frame is not None
    carried = _shot_workflow_input(fixture, shot, keyframe_asset_id=frame.asset_id)
    resolved = _keyframe_image_run(fixture.session, carried, shot)
    assert resolved is not None
    item = fixture.session.get(ImageGenerationItem, frame.item_id)
    assert item is not None
    assert resolved.id == item.run_id

    stranger = _shot_workflow_input(fixture, shot, keyframe_asset_id=shot.id)
    assert _keyframe_image_run(fixture.session, stranger, shot) is None
