"""End-to-end T13 tests against the deterministic director. No provider calls."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from services.storyboard.canonicalize import seconds_to_us
from services.storyboard.fake_provider import FakeStoryboardDirector
from services.storyboard.pipeline import (
    StoryboardPipeline,
    StoryboardValidationFailed,
)
from services.storyboard.providers import (
    CONTINUOUS_PROFILE,
    DISCRETE_PROFILE,
    CapabilityProfileError,
    load_capability_profile,
)
from tests.storyboard_fixtures import (
    add_project,
    build_fixture,
    reopen_fixture,
    segment_duration_us,
)
from vidgen.contracts.storyboard import (
    Storyboard,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    TimingManifest,
)
from vidgen.db.cost_models import (
    CostLedgerEntry,
    CostReservation,
    PricingVersion,
    ProjectBudget,
    ProviderAttempt,
    ProviderPriceRate,
)
from vidgen.db.models import Asset
from vidgen.db.storyboard_models import (
    StoryboardRepairAttempt,
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.storyboard_repository import StoryboardLineageError, StoryboardRepository


def run_pipeline(fixture, *, key="storyboard-key", director=None, **kwargs):
    pipeline = StoryboardPipeline(
        fixture.session, fixture.blobs, director or FakeStoryboardDirector(), **kwargs
    )
    return asyncio.run(pipeline.process(project_id=fixture.project.id, idempotency_key=key))


def load_storyboard(fixture, result) -> Storyboard:
    asset = fixture.session.get(Asset, result.storyboard_asset_id)
    return Storyboard.model_validate(json.loads(fixture.blobs.read(asset.storage_key)))


def load_manifest(fixture, result) -> TimingManifest:
    asset = fixture.session.get(Asset, result.timing_manifest_asset_id)
    return TimingManifest.model_validate(json.loads(fixture.blobs.read(asset.storage_key)))


# -- authoritative input selection -------------------------------------------


def test_pipeline_selects_the_authoritative_upstream_inputs(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    assert result.status == "storyboard_complete"
    run = fixture.session.get(StoryboardRun, result.storyboard_run_id)
    assert run.episode_model_id == fixture.episode_model.id
    assert run.script_id == fixture.script.id
    assert run.narration_run_id == fixture.narration_run.id
    assert run.selected is True
    assert fixture.project.status == "storyboard_complete"


def test_unapproved_script_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    fixture.script.status = "draft"
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "script_unapproved"


def test_stale_episode_model_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord

    fixture.session.add(
        EpisodeAnalysisRecord(
            project_id=fixture.project.id,
            analysis_run_id=uuid4(),
            version=2,
            canonical_analysis_asset_id=fixture.episode_model.canonical_analysis_asset_id,
            input_hash="9" * 64,
            duration_ms=900_000,
            character_count=1,
            location_count=1,
            scene_count=1,
            plot_beat_count=1,
            selected=False,
        )
    )
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "episode_model_stale"


def test_narration_from_a_different_script_version_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    fixture.narration_run.script_version = 2
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "narration_script_mismatch"
    assert "different approved script version" in error.value.message


def test_another_projects_selected_inputs_are_never_used(tmp_path: Path) -> None:
    """Two projects share one database; project A must not borrow project B's rows."""
    fixture = build_fixture(tmp_path)
    other = add_project(fixture.session, fixture.blobs, name="other project")
    fixture.session.commit()
    assert other.project.id != fixture.project.id

    # Project B has a selected, approved script and a complete narration run of its
    # own. Removing project A's selection must fail rather than fall through to B.
    fixture.script.selected = False
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "script_unselected"

    # With project A restored, its run must bind only to project A's lineage.
    fixture.script.selected = True
    fixture.session.commit()
    result = run_pipeline(fixture)
    run = fixture.session.get(StoryboardRun, result.storyboard_run_id)
    assert run.project_id == fixture.project.id
    assert run.script_id == fixture.script.id
    assert run.episode_model_id == fixture.episode_model.id
    assert run.narration_run_id == fixture.narration_run.id
    assert run.script_id != other.script.id
    assert run.narration_run_id != other.narration_run.id
    storyboard = load_storyboard(fixture, result)
    other_segment_ids = {segment.id for segment in other.script_segments}
    assert not {shot.script_segment_id for shot in storyboard.shots} & other_segment_ids
    assert not {shot.narration_segment_id for shot in storyboard.shots} & {
        segment.id for segment in other.narration_segments
    }
    # Characters and locations come from project A's episode model only.
    referenced = {
        character_id for shot in storyboard.shots for character_id in shot.character_reference_ids
    }
    assert referenced <= set(fixture.character_ids)
    assert not referenced & set(other.character_ids)


def test_another_projects_episode_model_does_not_make_this_one_stale(tmp_path: Path) -> None:
    """A newer episode-model version in a *different* project is not staleness here."""
    fixture = build_fixture(tmp_path)
    other = add_project(fixture.session, fixture.blobs, name="other project")
    other.episode_model.version = 99
    fixture.session.commit()
    result = run_pipeline(fixture)
    assert result.status == "storyboard_complete"


def test_incomplete_narration_run_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    fixture.narration_segments[1].status = "pending"
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "narration_segment_incomplete"


def test_unmeasured_narration_duration_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    fixture.narration_segments[0].duration_seconds = None
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "narration_duration_unmeasured"


def test_missing_word_timings_are_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    fixture.narration_segments[0].word_timings = []
    fixture.session.commit()
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture)
    assert error.value.code == "narration_word_timings_missing"


# -- capability profiles ------------------------------------------------------


def test_capability_profile_hash_is_stable_and_content_bound() -> None:
    first = load_capability_profile("runway-gen4-turbo")
    second = load_capability_profile("runway-gen4-turbo")
    assert first.capability_hash == second.capability_hash
    assert first.capability_hash != DISCRETE_PROFILE.capability_hash
    assert len(first.capability_hash) == 64


def test_unknown_capability_profile_fails_deterministically() -> None:
    with pytest.raises(CapabilityProfileError, match="unknown visual-provider"):
        load_capability_profile("no-such-profile")


# -- timing and coverage ------------------------------------------------------


def test_storyboard_covers_measured_narration_exactly(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    expected = sum(
        seconds_to_us(segment.duration_seconds) for segment in fixture.narration_segments
    )
    assert storyboard.total_duration_us == expected
    assert sum(shot.usable_duration_us for shot in storyboard.shots) == expected
    cursor = 0
    for shot in storyboard.shots:
        assert shot.global_start_us == cursor
        cursor = shot.global_end_us
    assert cursor == expected


def test_timing_manifest_matches_the_canonical_storyboard(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    manifest = load_manifest(fixture, result)
    assert manifest.total_usable_duration_us == manifest.total_narration_duration_us
    assert [entry.shot_id for entry in manifest.entries] == [
        shot.shot_id for shot in storyboard.shots
    ]
    assert manifest.segment_boundaries_us[0] == 0
    assert manifest.segment_boundaries_us[-1] == storyboard.total_duration_us
    assert manifest.adjustments


def test_discrete_provider_rounds_up_and_records_trimming(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture, capability_profile_id=DISCRETE_PROFILE.capability_profile_id)
    storyboard = load_storyboard(fixture, result)
    for shot in storyboard.shots:
        assert shot.requested_generation_duration_us in (
            DISCRETE_PROFILE.supported_generation_durations_us
        )
        assert shot.requested_generation_duration_us >= shot.usable_duration_us
        assert shot.trim_start_us + shot.trim_end_us == (
            shot.requested_generation_duration_us - shot.usable_duration_us
        )


def test_a_single_short_segment_produces_one_shot(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, texts=("Short and sweet.",))
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    assert result.segment_count == 1
    assert len(storyboard.shots) == 1
    assert storyboard.shots[0].usable_duration_us == segment_duration_us("Short and sweet.")


def test_a_long_segment_produces_multiple_shots(tmp_path: Path) -> None:
    long_text = " ".join(["word"] * 40) + "."
    fixture = build_fixture(tmp_path, texts=(long_text,))
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    assert len(storyboard.shots) > 1
    assert sum(s.usable_duration_us for s in storyboard.shots) == segment_duration_us(long_text)


# -- determinism and idempotency ---------------------------------------------


def test_identical_inputs_produce_a_byte_identical_storyboard(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first = run_pipeline(fixture)
    first_asset = fixture.session.get(Asset, first.storyboard_asset_id)
    first_sha = first_asset.sha256
    first_manifest = fixture.session.get(Asset, first.timing_manifest_asset_id).sha256

    session, blobs = reopen_fixture(fixture, tmp_path, tmp_path / "replay")
    replay = build_replay(fixture, session, blobs)
    second = run_pipeline(replay)
    assert session.get(Asset, second.storyboard_asset_id).sha256 == first_sha
    assert session.get(Asset, second.timing_manifest_asset_id).sha256 == first_manifest
    assert second.storyboard_run_id == first.storyboard_run_id


def build_replay(fixture, session, blobs):
    """A fixture handle bound to the copied database."""
    from dataclasses import replace

    project = session.get(type(fixture.project), fixture.project.id)
    return replace(fixture, session=session, blobs=blobs, project=project)


def test_completed_run_is_returned_without_new_provider_work(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first = run_pipeline(fixture)
    attempts = fixture.session.query(ProviderAttempt).count()
    rows = fixture.session.query(StoryboardShotRecord).count()
    assets = fixture.session.query(Asset).count()

    second = run_pipeline(fixture)
    assert second.storyboard_run_id == first.storyboard_run_id
    assert second.storyboard_asset_id == first.storyboard_asset_id
    assert fixture.session.query(ProviderAttempt).count() == attempts
    assert fixture.session.query(StoryboardShotRecord).count() == rows
    assert fixture.session.query(Asset).count() == assets


def test_reusing_an_idempotency_key_with_new_inputs_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    run_pipeline(fixture)
    with pytest.raises(StoryboardLineageError) as error:
        run_pipeline(fixture, capability_profile_id=DISCRETE_PROFILE.capability_profile_id)
    assert error.value.code == "idempotency_key_reused"


# -- restart, repair, and failure --------------------------------------------


class _InterruptingDirector(FakeStoryboardDirector):
    """Fails after the first segment so the run can be resumed."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        self.calls += 1
        if self.calls > 1:
            raise TimeoutError("provider timed out")
        return await super().propose(request)


def test_interrupted_run_resumes_without_redirecting_valid_segments(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    interrupting = _InterruptingDirector()
    with pytest.raises(TimeoutError):
        run_pipeline(fixture, director=interrupting)
    run = fixture.session.scalar(select(StoryboardRun))
    assert run.status == "storyboard_failed"
    assert fixture.project.status == "storyboard_failed"
    completed = [
        row.sequence
        for row in fixture.session.scalars(select(StoryboardSegmentCheckpoint))
        if row.status == "complete"
    ]
    assert completed == [0]

    resumed = FakeStoryboardDirector()
    calls: list[int] = []
    original = resumed.propose

    async def counting(request):
        calls.append(request.segment_sequence)
        return await original(request)

    resumed.propose = counting  # type: ignore[method-assign]
    result = run_pipeline(fixture, director=resumed)
    assert result.status == "storyboard_complete"
    # Segment 0 was recovered from its checkpoint, not re-directed.
    assert calls == [1]


class _RepairableDirector(FakeStoryboardDirector):
    """Requests too many characters on the first attempt for one segment."""

    def __init__(self, failing_sequence: int = 0) -> None:
        super().__init__()
        self.failing_sequence = failing_sequence
        self.requests: list[tuple[int, int]] = []

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        self.requests.append((request.segment_sequence, request.attempt_number))
        result = await super().propose(request)
        if request.segment_sequence == self.failing_sequence and request.attempt_number == 1:
            extras = [uuid4() for _ in range(6)]
            proposals = [
                proposal.model_copy(update={"character_reference_ids": extras})
                for proposal in result.proposals
            ]
            return result.model_copy(update={"proposals": proposals})
        return result


def test_excessive_character_count_is_repaired_for_one_segment_only(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    director = _RepairableDirector(failing_sequence=1)
    result = run_pipeline(fixture, director=director)
    assert result.status == "storyboard_complete"
    assert result.repair_attempt_count == 1
    # Segment 0 was directed once; only segment 1 was re-directed.
    assert director.requests == [(0, 1), (1, 1), (1, 2)]
    repairs = list(fixture.session.scalars(select(StoryboardRepairAttempt)))
    assert len(repairs) == 1
    assert repairs[0].status == "valid"
    assert repairs[0].attempt_number == 1
    codes = {item["code"] for item in repairs[0].input_diagnostics}
    assert "excessive_character_count" in codes or "too_many_references" in codes


class _AlwaysInvalidDirector(FakeStoryboardDirector):
    """Never stops referencing characters that do not exist."""

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        result = await super().propose(request)
        ghost = [uuid4()]
        proposals = [
            item.model_copy(
                update={
                    "character_reference_ids": ghost,
                    "incoming_continuity": item.incoming_continuity.model_copy(
                        update={
                            "present_character_ids": ghost,
                            "character_appearance_states": [],
                            "subject_positions": [],
                        }
                    ),
                    "expected_outgoing_continuity": (
                        item.expected_outgoing_continuity.model_copy(
                            update={
                                "present_character_ids": ghost,
                                "character_appearance_states": [],
                                "subject_positions": [],
                            }
                        )
                    ),
                }
            )
            for item in result.proposals
        ]
        return result.model_copy(update={"proposals": proposals})


def test_unrepairable_segment_fails_after_bounded_attempts(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with pytest.raises(StoryboardValidationFailed) as error:
        run_pipeline(fixture, director=_AlwaysInvalidDirector())
    assert "invalid_character_reference" in str(error.value)
    checkpoint = fixture.session.scalar(select(StoryboardSegmentCheckpoint))
    assert checkpoint.attempt_count == 3  # one direction plus two bounded repairs
    assert fixture.project.status == "storyboard_failed"


def test_missing_evidence_reference_is_diagnosed(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)

    class _GhostEvidenceDirector(FakeStoryboardDirector):
        async def propose(self, request):
            from vidgen.contracts.storyboard import StoryboardSourceReference

            result = await super().propose(request)
            ghost = [
                StoryboardSourceReference(reference_type="scene_evidence", reference_id=uuid4())
            ]
            return result.model_copy(
                update={
                    "proposals": [
                        item.model_copy(update={"evidence_references": ghost})
                        for item in result.proposals
                    ]
                }
            )

    with pytest.raises(StoryboardValidationFailed) as error:
        run_pipeline(fixture, director=_GhostEvidenceDirector())
    assert "missing_evidence_reference" in str(error.value)


class _CountingDirector(FakeStoryboardDirector):
    """Records every (segment, attempt) the pipeline actually sends to a provider."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[int, int]] = []

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        self.requests.append((request.segment_sequence, request.attempt_number))
        return await super().propose(request)


def test_rebuilt_checkpoint_replays_without_violating_its_attempt_counters(
    tmp_path: Path,
) -> None:
    """A repaired segment invalidates later checkpoints; they must replay cleanly.

    Regression: the rebuilt checkpoints kept their old ``repair_attempt_count``
    while replaying from attempt one, which violates the checkpoint's own
    ``repair_attempt_count <= attempt_count`` constraint on the next commit.
    """
    fixture = build_fixture(tmp_path)
    # Segment 1 needs a repair, so it ends the first run with counters above one.
    first = run_pipeline(fixture, director=_RepairableDirector(failing_sequence=1), key="run-1")
    assert first.repair_attempt_count == 1
    checkpoint = fixture.session.scalar(
        select(StoryboardSegmentCheckpoint)
        .where(StoryboardSegmentCheckpoint.storyboard_run_id == first.storyboard_run_id)
        .where(StoryboardSegmentCheckpoint.sequence == 1)
    )
    assert (checkpoint.attempt_count, checkpoint.repair_attempt_count) == (2, 1)

    # Force that checkpoint's inputs to differ, and reopen the run the way an
    # interrupted resume would, so the next pass actually rebuilds the checkpoint.
    checkpoint.input_hash = "0" * 64
    fixture.session.get(StoryboardRun, first.storyboard_run_id).status = "storyboard_failed"
    fixture.session.commit()

    second = run_pipeline(fixture, director=FakeStoryboardDirector(), key="run-1")
    assert second.status == "storyboard_complete"
    rebuilt = fixture.session.get(StoryboardSegmentCheckpoint, checkpoint.id)
    assert rebuilt.status == "complete"
    assert rebuilt.repair_attempt_count <= rebuilt.attempt_count
    assert rebuilt.repair_attempt_count == 0
    # Superseded repair rows do not outlive the plan they described.
    assert (
        not fixture.session.query(StoryboardRepairAttempt)
        .filter_by(segment_checkpoint_id=checkpoint.id)
        .count()
    )


def test_renumbering_replay_reuses_validated_results_without_new_charges(
    tmp_path: Path,
) -> None:
    """Rebuilding later checkpoints must not re-pay for work already validated.

    Regression: the stored provider result was keyed on the attempt-numbered
    idempotency key, so a replay starting at attempt one missed a result produced
    on attempt two and submitted a fresh, billable request.
    """
    fixture = build_fixture(tmp_path)
    _price_the_director(fixture, hard_cap=Decimal("100"), warning_cap=Decimal("90"))
    # Segment 0 takes two attempts, so its validated result carries attempt 2.
    run_pipeline(fixture, director=_RepairableDirector(failing_sequence=0), key="run-1")
    attempts = fixture.session.query(ProviderAttempt).count()
    committed = fixture.session.scalar(select(ProjectBudget)).committed_amount
    checkpoints = fixture.session.scalars(
        select(StoryboardSegmentCheckpoint).order_by(StoryboardSegmentCheckpoint.sequence)
    ).all()
    assert checkpoints[0].provider_result["attempt"] == 2
    assert checkpoints[0].provider_result["validated"] is True

    # Invalidate the *last* segment so the run rebuilds it and replays segment 0's
    # numbering; segment 0's validated result must be reused, not re-requested.
    run = fixture.session.scalar(select(StoryboardRun))
    checkpoints[-1].input_hash = "1" * 64
    run.status = "storyboard_failed"
    fixture.session.commit()

    director = _CountingDirector()
    result = run_pipeline(fixture, director=director, key="run-1")
    assert result.status == "storyboard_complete"
    # Only the invalidated segment was re-directed.
    assert director.requests == [(len(checkpoints) - 1, 1)]
    # The re-directed segment resolves back to its own durable attempt identity, so
    # even the request that was re-issued reuses its T23 attempt and reservation
    # rather than opening a second billable one.
    assert fixture.session.query(ProviderAttempt).count() == attempts
    assert fixture.session.scalar(select(ProjectBudget)).committed_amount == committed


def test_a_failed_attempt_is_never_reused_as_a_recovered_result(tmp_path: Path) -> None:
    """The repair loop must re-direct, not replay the result it just rejected."""
    fixture = build_fixture(tmp_path)
    director = _RepairableDirector(failing_sequence=0)
    result = run_pipeline(fixture, director=director)
    assert result.status == "storyboard_complete"
    # Attempt 1 failed validation, so attempt 2 was a genuine second request.
    assert (0, 1) in director.requests
    assert (0, 2) in director.requests


# -- continuity ---------------------------------------------------------------


def test_continuity_propagates_across_segments(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    for previous, current in zip(storyboard.shots, storyboard.shots[1:], strict=False):
        assert (
            previous.expected_outgoing_continuity.location_id
            == current.incoming_continuity.location_id
        )
    assert storyboard.shots[0].incoming_continuity.location_id in fixture.location_ids


def test_anonymous_speaker_never_receives_a_character_identity(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, anonymous_segments=frozenset({0}))
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    anonymous_segment = fixture.script_segments[0].id
    anonymous_shots = [s for s in storyboard.shots if s.script_segment_id == anonymous_segment]
    assert anonymous_shots
    assert all(not shot.character_reference_ids for shot in anonymous_shots)
    named_shots = [s for s in storyboard.shots if s.script_segment_id != anonymous_segment]
    assert any(shot.character_reference_ids for shot in named_shots)


def test_continuity_contradiction_is_diagnosed_and_repaired(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)

    class _ContradictingDirector(FakeStoryboardDirector):
        """Changes time of day between consecutive shots with no explanation."""

        async def propose(self, request):
            result = await super().propose(request)
            proposals = list(result.proposals)
            if len(proposals) > 1:
                proposals[0] = proposals[0].model_copy(
                    update={
                        "expected_outgoing_continuity": (
                            proposals[0].expected_outgoing_continuity.model_copy(
                                update={"time_of_day": "morning"}
                            )
                        )
                    }
                )
                proposals[1] = proposals[1].model_copy(
                    update={
                        "incoming_continuity": proposals[1].incoming_continuity.model_copy(
                            update={"time_of_day": "night"}
                        )
                    }
                )
            return result.model_copy(update={"proposals": proposals})

    result = run_pipeline(fixture, director=_ContradictingDirector())
    # The contradiction is caught, and the targeted repair resolves it.
    assert result.status == "storyboard_complete"
    repairs = list(fixture.session.scalars(select(StoryboardRepairAttempt)))
    assert repairs
    codes = {diagnostic["code"] for repair in repairs for diagnostic in repair.input_diagnostics}
    assert "continuity_contradiction" in codes
    assert all(repair.status == "valid" for repair in repairs)


def test_an_explained_continuity_change_is_not_a_contradiction(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)

    class _ExplainedDirector(FakeStoryboardDirector):
        async def propose(self, request):
            from vidgen.contracts.episode_analysis import StructuredNote

            result = await super().propose(request)
            proposals = list(result.proposals)
            if len(proposals) > 1:
                proposals[0] = proposals[0].model_copy(
                    update={
                        "expected_outgoing_continuity": (
                            proposals[0].expected_outgoing_continuity.model_copy(
                                update={"time_of_day": "morning"}
                            )
                        )
                    }
                )
                proposals[1] = proposals[1].model_copy(
                    update={
                        "incoming_continuity": proposals[1].incoming_continuity.model_copy(
                            update={
                                "time_of_day": "night",
                                "unresolved_warnings": [
                                    StructuredNote(
                                        code="time_of_day",
                                        message="deliberate time jump between beats",
                                    )
                                ],
                            }
                        )
                    }
                )
            return result.model_copy(update={"proposals": proposals})

    result = run_pipeline(fixture, director=_ExplainedDirector())
    assert result.status == "storyboard_complete"
    assert result.repair_attempt_count == 0


class _ReorderingDirector(FakeStoryboardDirector):
    """Emits the same continuity with every collection reversed."""

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        result = await super().propose(request)
        state = result.expected_outgoing_continuity
        return result.model_copy(
            update={
                "expected_outgoing_continuity": state.model_copy(
                    update={
                        "present_character_ids": list(reversed(state.present_character_ids)),
                        "character_appearance_states": list(
                            reversed(state.character_appearance_states)
                        ),
                        "subject_positions": list(reversed(state.subject_positions)),
                    }
                )
            }
        )


def test_reordered_result_continuity_does_not_invalidate_later_segments(
    tmp_path: Path,
) -> None:
    """Regression: result-level continuity was hashed but never canonicalized.

    The outgoing state feeds the next segment's input identity, so a merely
    reordered character list used to change that hash and force a rebuild of every
    downstream checkpoint.
    """

    fixture = build_fixture(tmp_path)
    baseline = run_pipeline(fixture, director=FakeStoryboardDirector())
    ordered_hashes = [
        row.input_hash
        for row in StoryboardRepository(fixture.session).checkpoints(baseline.storyboard_run_id)
    ]

    # Same database, same inputs, a director that only reorders its outgoing state.
    session, blobs = reopen_fixture(fixture, tmp_path, tmp_path / "reordered")
    replay = build_replay(fixture, session, blobs)
    variant = run_pipeline(replay, director=_ReorderingDirector(), key="reordered")
    variant_hashes = [
        row.input_hash
        for row in StoryboardRepository(session).checkpoints(variant.storyboard_run_id)
    ]
    # Segment identity binds incoming continuity, which is now canonical, so
    # ordering noise from the director cannot shift any downstream checkpoint.
    assert ordered_hashes == variant_hashes


def test_contradictory_result_level_outgoing_continuity_is_caught(tmp_path: Path) -> None:
    """Regression: the cross-segment handoff was adopted without validation.

    The result-level outgoing state becomes the next segment's incoming state, so
    letting it drift from the final proposal's own outgoing state allowed a
    contradiction to cross a segment boundary unchecked.
    """

    class _DriftingDirector(FakeStoryboardDirector):
        async def propose(self, request):
            result = await super().propose(request)
            return result.model_copy(
                update={
                    "expected_outgoing_continuity": (
                        result.expected_outgoing_continuity.model_copy(
                            update={"time_of_day": "night", "screen_direction": "right_to_left"}
                        )
                    )
                }
            )

    with pytest.raises(StoryboardValidationFailed) as error:
        run_pipeline(fixture := build_fixture(tmp_path), director=_DriftingDirector())
    assert "continuity_contradiction" in str(error.value)
    assert fixture.project.status == "storyboard_failed"


def test_a_resumed_repair_does_not_re_pay_for_a_rejected_attempt(tmp_path: Path) -> None:
    """Regression: resuming restarted the repair loop at attempt one.

    That re-submitted a provider call whose result was already known to have
    failed validation. The stored report carries those diagnostics, so the loop
    resumes at the attempt the checkpoint actually reached.
    """
    fixture = build_fixture(tmp_path)
    # Interrupt the run *during* the repair of segment 0.
    interrupting = _RepairThenFailDirector()
    with pytest.raises(TimeoutError):
        run_pipeline(fixture, director=interrupting, key="run-1")
    checkpoint = fixture.session.scalar(
        select(StoryboardSegmentCheckpoint).where(StoryboardSegmentCheckpoint.sequence == 0)
    )
    # Attempt 1 failed validation and attempt 2 was checkpointed before the
    # provider died, so the stored report is attempt 1's rejection.
    assert checkpoint.attempt_count == 2
    assert checkpoint.validation_report is not None
    assert checkpoint.validation_report["valid"] is False

    resumed = _CountingDirector()
    result = run_pipeline(fixture, director=resumed, key="run-1")
    assert result.status == "storyboard_complete"
    # Attempt 1 for segment 0 completed and failed validation, so it is not
    # replayed. Attempt 2 was started but never got a response, so it is retried
    # rather than skipped - the repair budget is not silently spent.
    assert (0, 1) not in resumed.requests
    assert (0, 2) in resumed.requests


class _RepairThenFailDirector(FakeStoryboardDirector):
    """Fails segment 0's first attempt, then dies before the repair completes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        self.calls += 1
        if self.calls > 1:
            raise TimeoutError("provider timed out during the repair")
        result = await super().propose(request)
        extras = [uuid4() for _ in range(6)]
        return result.model_copy(
            update={
                "proposals": [
                    item.model_copy(update={"character_reference_ids": extras})
                    for item in result.proposals
                ]
            }
        )


def test_timing_manifest_gets_its_own_content_key(tmp_path: Path) -> None:
    """Regression: manifest and report keys were derived from the storyboard hash.

    A manifest that differs while the storyboard is byte-identical then collided
    on a stale key and raised a bare ValueError out of AssetService.
    """
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard_asset = fixture.session.get(Asset, result.storyboard_asset_id)
    manifest_asset = fixture.session.get(Asset, result.timing_manifest_asset_id)
    assert storyboard_asset.idempotency_key != manifest_asset.idempotency_key
    assert storyboard_asset.idempotency_key.endswith(
        storyboard_asset.generation_parameters["output_hash"]
    )
    # The manifest key is bound to the manifest's own bytes, not the storyboard's.
    assert not manifest_asset.idempotency_key.endswith(
        storyboard_asset.generation_parameters["output_hash"]
    )


def test_validation_report_is_kept_whenever_it_carries_diagnostics(tmp_path: Path) -> None:
    """A run can succeed with warning diagnostics; those must not be discarded."""
    fixture = build_fixture(tmp_path)
    clean = run_pipeline(fixture, key="clean")
    # No diagnostics and a small report, so no artifact is worth storing.
    assert clean.validation_report_asset_id is None


# -- assets, provenance, and persistence -------------------------------------


def test_assets_record_full_upstream_provenance(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard_asset = fixture.session.get(Asset, result.storyboard_asset_id)
    parents = {parent.id for parent in storyboard_asset.parents}
    assert fixture.episode_model.canonical_analysis_asset_id in parents
    assert fixture.script.canonical_script_asset_id in parents
    assert fixture.narration_run.preview_asset_id in parents
    for segment in fixture.narration_segments:
        assert segment.normalized_asset_id in parents
    parameters = storyboard_asset.generation_parameters
    assert parameters["capability_hash"] == CONTINUOUS_PROFILE.capability_hash
    assert (
        parameters["input_hash"]
        == fixture.session.get(StoryboardRun, result.storyboard_run_id).input_hash
    )
    assert parameters["provider_request_ids"]
    manifest_asset = fixture.session.get(Asset, result.timing_manifest_asset_id)
    assert storyboard_asset.id in {parent.id for parent in manifest_asset.parents}


def test_persisted_shots_match_the_canonical_contract(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    result = run_pipeline(fixture)
    storyboard = load_storyboard(fixture, result)
    rows = list(
        fixture.session.scalars(
            select(StoryboardShotRecord).order_by(StoryboardShotRecord.global_sequence)
        )
    )
    assert [row.stable_shot_id for row in rows] == [shot.shot_id for shot in storyboard.shots]
    assert [row.global_sequence for row in rows] == list(range(len(rows)))
    for row, shot in zip(rows, storyboard.shots, strict=True):
        assert row.usable_duration_us == shot.usable_duration_us
        assert row.requested_generation_duration_us == shot.requested_generation_duration_us
        assert row.trim_start_us + row.trim_end_us == (
            row.requested_generation_duration_us - row.usable_duration_us
        )


def test_only_one_storyboard_is_selected_per_upstream_version(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first = run_pipeline(fixture, key="first")
    second = run_pipeline(fixture, key="second")
    assert first.storyboard_run_id != second.storyboard_run_id
    selected = [row.id for row in fixture.session.scalars(select(StoryboardRun)) if row.selected]
    assert selected == [second.storyboard_run_id]


# -- T23 cost and telemetry ---------------------------------------------------


def _price_the_director(fixture, *, hard_cap: Decimal, warning_cap: Decimal) -> None:
    now = datetime.now(UTC)
    version = PricingVersion(
        name="storyboard-test",
        currency="USD",
        source_metadata={"source": "fixture"},
        verification_date=now.date(),
        activated_at=now,
    )
    fixture.session.add(version)
    fixture.session.flush()
    for unit in ("INPUT_TOKEN", "OUTPUT_TOKEN"):
        fixture.session.add(
            ProviderPriceRate(
                pricing_version_id=version.id,
                provider="fake",
                model="fake-storyboard-1",
                operation="storyboard.direct",
                usage_unit=unit,
                unit_size=Decimal("1000"),
                unit_price=Decimal("0.01"),
                effective_start=now,
                active=True,
                source_reference="fixture://pricing",
            )
        )
    fixture.session.add(
        ProjectBudget(
            project_id=fixture.project.id,
            hard_cap=hard_cap,
            warning_cap=warning_cap,
            policy_version="v1",
            reserved_amount=Decimal("0"),
            committed_amount=Decimal("0"),
            currency="USD",
        )
    )
    fixture.session.commit()


def test_budget_reservation_and_reconciliation(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _price_the_director(fixture, hard_cap=Decimal("100"), warning_cap=Decimal("90"))
    result = run_pipeline(fixture)
    assert result.status == "storyboard_complete"
    attempts = list(fixture.session.scalars(select(ProviderAttempt)))
    assert len(attempts) == len(fixture.script_segments)
    assert all(attempt.status == "SUCCEEDED" for attempt in attempts)
    assert all(attempt.operation == "storyboard.direct" for attempt in attempts)
    assert all(attempt.provider_request_id for attempt in attempts)
    reservations = list(fixture.session.scalars(select(CostReservation)))
    assert len(reservations) == len(attempts)
    assert Decimal(result.estimated_cost) > 0
    assert Decimal(result.actual_cost) > 0
    budget = fixture.session.scalar(select(ProjectBudget))
    assert budget.committed_amount > 0
    assert budget.reserved_amount == Decimal("0")


def test_budget_denial_stops_the_run_before_any_shot_is_persisted(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _price_the_director(fixture, hard_cap=Decimal("0.0000001"), warning_cap=Decimal("0"))
    from vidgen.db.cost_repository import BudgetExceededError

    with pytest.raises(BudgetExceededError):
        run_pipeline(fixture)
    assert fixture.session.query(StoryboardShotRecord).count() == 0
    run = fixture.session.scalar(select(StoryboardRun))
    assert run.status == "storyboard_failed"
    assert run.error_code == "BudgetExceededError"


def test_retrying_a_completed_run_creates_no_duplicate_charges(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _price_the_director(fixture, hard_cap=Decimal("100"), warning_cap=Decimal("90"))
    run_pipeline(fixture)
    attempts = fixture.session.query(ProviderAttempt).count()
    reservations = fixture.session.query(CostReservation).count()
    ledger = fixture.session.query(CostLedgerEntry).count()
    committed = fixture.session.scalar(select(ProjectBudget)).committed_amount

    run_pipeline(fixture)
    assert fixture.session.query(ProviderAttempt).count() == attempts
    assert fixture.session.query(CostReservation).count() == reservations
    assert fixture.session.query(CostLedgerEntry).count() == ledger
    assert fixture.session.scalar(select(ProjectBudget)).committed_amount == committed
