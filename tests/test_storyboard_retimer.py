"""Deterministic timing-solver tests. No database, no provider, no I/O."""

from __future__ import annotations

import pytest

from services.storyboard.canonicalize import seconds_to_us
from services.storyboard.providers import (
    CONTINUOUS_PROFILE,
    DISCRETE_PROFILE,
    build_capability_profile,
)
from services.storyboard.retimer import (
    RetimerConfig,
    RetimerError,
    allocate_residual,
    retime_segment,
    select_generation_duration,
)
from vidgen.contracts.storyboard import (
    ActionPlan,
    CameraPlan,
    ContinuityState,
    NarrationBoundary,
    StoryboardShotProposal,
    TransitionPlan,
)

SECOND = 1_000_000


def timings(count: int, *, step_us: int = 500_000) -> list[NarrationBoundary]:
    return [
        NarrationBoundary(word_index=index, offset_us=(index + 1) * step_us, kind="word")
        for index in range(count)
    ]


def proposal(
    sequence: int,
    start: int,
    end: int,
    duration_us: int,
    *,
    transition_out: TransitionPlan | None = None,
    transition_in: TransitionPlan | None = None,
) -> StoryboardShotProposal:
    state = ContinuityState()
    return StoryboardShotProposal(
        proposal_sequence=sequence,
        visual_objective="objective",
        desired_duration_us=duration_us,
        word_start_index=start,
        word_end_index=end,
        camera=CameraPlan(
            framing="medium", angle="eye_level", movement="static", movement_intensity="none"
        ),
        action=ActionPlan(subject_action="does something", beat_intent="continue"),
        transition_in=transition_in or TransitionPlan(kind="cut"),
        transition_out=transition_out or TransitionPlan(kind="cut"),
        incoming_continuity=state,
        expected_outgoing_continuity=state,
    )


def solve(
    proposals, word_count, duration_us, *, capability=CONTINUOUS_PROFILE, config=None, approved=None
):
    return retime_segment(
        segment_sequence=0,
        narration_duration_us=duration_us,
        word_timings=timings(word_count),
        approved_boundaries=approved or [],
        proposals=proposals,
        capability=capability,
        config=config or RetimerConfig(),
    )


def test_seconds_to_microseconds_is_exact() -> None:
    # 0.1 + 0.2 style drift never reaches the canonical timeline.
    assert seconds_to_us(6.5) == 6_500_000
    assert seconds_to_us("0.123456") == 123_456
    assert sum(seconds_to_us(x) for x in (0.1, 0.2)) == 300_000


def test_allocate_residual_is_deterministic_and_exact() -> None:
    shares = allocate_residual(10, 3)
    assert shares == [4, 3, 3]
    assert sum(shares) == 10
    assert allocate_residual(10, 3) == shares
    with pytest.raises(ValueError):
        allocate_residual(10, 0)


def test_one_segment_can_produce_one_shot() -> None:
    result = solve([proposal(0, 0, 4, 2 * SECOND)], 4, 2 * SECOND)
    assert len(result.shots) == 1
    shot = result.shots[0]
    assert (shot.start_us, shot.end_us) == (0, 2 * SECOND)
    assert shot.usable_duration_us == 2 * SECOND


def test_one_segment_can_produce_multiple_shots() -> None:
    result = solve([proposal(0, 0, 4, 2 * SECOND), proposal(1, 4, 8, 2 * SECOND)], 8, 4 * SECOND)
    assert [(s.start_us, s.end_us) for s in result.shots] == [
        (0, 2 * SECOND),
        (2 * SECOND, 4 * SECOND),
    ]


def test_measured_narration_duration_is_the_authority() -> None:
    # The director asked for 9 s of shots against 4 s of measured narration.
    result = solve([proposal(0, 0, 4, 5 * SECOND), proposal(1, 4, 8, 4 * SECOND)], 8, 4 * SECOND)
    assert sum(shot.usable_duration_us for shot in result.shots) == 4 * SECOND
    assert result.shots[-1].end_us == 4 * SECOND
    # Narration is never stretched to match the proposal.
    assert all(shot.usable_duration_us < 5 * SECOND for shot in result.shots)
    assert any(item.kind == "boundary_snap" for item in result.adjustments)


def test_no_gaps_and_no_overlaps() -> None:
    result = solve(
        [proposal(0, 0, 3, SECOND), proposal(1, 3, 6, SECOND), proposal(2, 6, 10, SECOND)],
        10,
        5 * SECOND,
    )
    cursor = 0
    for shot in result.shots:
        assert shot.start_us == cursor
        cursor = shot.end_us
    assert cursor == 5 * SECOND


def test_clause_boundary_split_is_preferred_over_a_plain_word() -> None:
    approved = [NarrationBoundary(word_index=15, offset_us=8 * SECOND, kind="clause")]
    result = solve(
        [proposal(0, 0, 30, 15 * SECOND)],
        30,
        15 * SECOND,
        approved=approved,
        config=RetimerConfig(max_shot_duration_us=8 * SECOND),
    )
    # 15 s exceeds the configured 8 s maximum, so the shot must split; the even
    # division point is 7.5 s but the approved clause boundary at 8 s wins.
    assert len(result.shots) == 2
    assert result.shots[0].end_us == 8 * SECOND
    assert any(item.kind == "split" for item in result.adjustments)


def test_beat_boundary_split_is_used_when_no_clause_exists() -> None:
    approved = [NarrationBoundary(word_index=13, offset_us=7 * SECOND, kind="beat")]
    result = solve(
        [proposal(0, 0, 30, 15 * SECOND)],
        30,
        15 * SECOND,
        approved=approved,
        config=RetimerConfig(max_shot_duration_us=8 * SECOND),
    )
    assert result.shots[0].end_us == 7 * SECOND


def test_unsupported_duration_rounds_up_to_the_next_supported_generation() -> None:
    result = solve([proposal(0, 0, 9, 4_500_000)], 9, 4_500_000, capability=DISCRETE_PROFILE)
    shot = result.shots[0]
    assert shot.usable_duration_us == 4_500_000
    assert shot.requested_generation_duration_us == 6 * SECOND
    assert shot.trim_start_us + shot.trim_end_us == 1_500_000
    assert any(item.kind == "generation_round_up" for item in result.adjustments)
    assert any(item.kind == "trim" for item in result.adjustments)


def test_trim_instructions_follow_the_configured_policy() -> None:
    centered = build_capability_profile(
        **{
            **{
                key: value
                for key, value in DISCRETE_PROFILE.model_dump().items()
                if key != "capability_hash"
            },
            "capability_profile_id": "veo-3.1-fast-centered",
            "trimming_policy": "trim_center",
        }
    )
    result = solve([proposal(0, 0, 9, 4_500_000)], 9, 4_500_000, capability=centered)
    shot = result.shots[0]
    assert (shot.trim_start_us, shot.trim_end_us) == (750_000, 750_000)
    # Repeating the solve gives the identical instruction.
    assert (
        solve([proposal(0, 0, 9, 4_500_000)], 9, 4_500_000, capability=centered)
        .shots[0]
        .trim_start_us
        == 750_000
    )


def test_supported_duration_selection_covers_both_provider_shapes() -> None:
    assert select_generation_duration(4_100_000, DISCRETE_PROFILE) == 6 * SECOND
    assert select_generation_duration(9 * SECOND, DISCRETE_PROFILE) is None
    assert select_generation_duration(3_310_000, CONTINUOUS_PROFILE) == 3_400_000
    assert select_generation_duration(500_000, CONTINUOUS_PROFILE) == SECOND


def test_zero_length_shots_are_rejected() -> None:
    with pytest.raises(RetimerError) as error:
        retime_segment(
            segment_sequence=0,
            narration_duration_us=0,
            word_timings=timings(2),
            approved_boundaries=[],
            proposals=[proposal(0, 0, 2, SECOND)],
            capability=CONTINUOUS_PROFILE,
            config=RetimerConfig(),
        )
    assert error.value.diagnostic.code == "nonpositive_duration"
    assert error.value.diagnostic.repairable is False


def test_word_range_gaps_are_rejected_as_repairable() -> None:
    with pytest.raises(RetimerError) as error:
        solve([proposal(0, 0, 3, SECOND), proposal(1, 5, 8, SECOND)], 8, 4 * SECOND)
    assert error.value.diagnostic.code == "word_range_gap"
    assert error.value.diagnostic.repairable is True


def test_duration_beyond_the_provider_maximum_without_a_boundary_is_rejected() -> None:
    # One word lasting 30 s against a profile whose maximum generation is 10 s.
    long_word = [NarrationBoundary(word_index=0, offset_us=30 * SECOND, kind="word")]
    result = retime_segment(
        segment_sequence=0,
        narration_duration_us=30 * SECOND,
        word_timings=long_word,
        approved_boundaries=[],
        proposals=[proposal(0, 0, 1, 30 * SECOND)],
        capability=CONTINUOUS_PROFILE,
        config=RetimerConfig(),
    )
    # No semantic boundary exists, so the solver divides evenly and says so.
    assert len(result.shots) == 4
    assert sum(shot.usable_duration_us for shot in result.shots) == 30 * SECOND
    assert result.warnings


def test_short_shots_merge_into_a_neighbour() -> None:
    config = RetimerConfig(min_shot_duration_us=2 * SECOND)
    result = solve(
        [proposal(0, 0, 1, 500_000), proposal(1, 1, 8, 3_500_000)],
        8,
        4 * SECOND,
        config=config,
    )
    assert len(result.shots) == 1
    assert result.shots[0].usable_duration_us == 4 * SECOND
    assert any(item.kind == "merge" for item in result.adjustments)


def test_transition_handles_are_generated_but_never_narration_coverage() -> None:
    dissolve = TransitionPlan(kind="dissolve", duration_us=200_000, handle_us=200_000)
    result = solve([proposal(0, 0, 8, 4 * SECOND, transition_out=dissolve)], 8, 4 * SECOND)
    shot = result.shots[0]
    assert shot.usable_duration_us == 4 * SECOND
    assert shot.transition_handle_us == 200_000
    assert shot.requested_generation_duration_us >= 4_200_000
    # The handle is trimmed material, so narration coverage is unchanged.
    assert shot.trim_start_us + shot.trim_end_us == (
        shot.requested_generation_duration_us - shot.usable_duration_us
    )


def test_solver_output_is_stable_for_identical_inputs() -> None:
    def once() -> list[tuple[int, int, int, int, int]]:
        result = solve(
            [proposal(0, 0, 10, 5 * SECOND), proposal(1, 10, 26, 8 * SECOND)],
            26,
            13 * SECOND,
            capability=DISCRETE_PROFILE,
        )
        return [
            (
                shot.start_us,
                shot.end_us,
                shot.requested_generation_duration_us,
                shot.trim_start_us,
                shot.trim_end_us,
            )
            for shot in result.shots
        ]

    assert once() == once()
