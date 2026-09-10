"""Shot pacing: presets guide semantic planning; the retimer stays the authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from services.generation.settings import (
    generation_policy_identity,
    resolve_generation_settings,
    with_generation_settings,
)
from services.storyboard.fake_provider import HERO_IMPORTANCE, FakeStoryboardDirector
from services.storyboard.pacing import (
    PACING_PROFILES,
    PacingProfile,
    pacing_profile,
    retimer_config_for,
)
from services.storyboard.providers import PROMPT_VERSION, RUNWAY_GEN4_TURBO_PROFILE
from services.storyboard.retimer import RetimerConfig, retime_segment
from tests.storyboard_fixtures import build_fixture, segment_duration_us
from tests.test_storyboard_pipeline import load_manifest, load_storyboard, run_pipeline
from vidgen.contracts.generation import GenerationQuality, ProjectGenerationSettings, ShotPacing
from vidgen.contracts.storyboard import (
    ContinuityState,
    NarrationBoundary,
    StoryboardProviderRequest,
)
from vidgen.db.storyboard_models import StoryboardRun

SECOND = 1_000_000

#: Six sentences at 500 ms per word: short enough that relaxed pacing joins
#: neighbours, long enough that fast pacing has clause boundaries to cut on.
NARRATION = (
    "Our hero wakes up late again this morning. "
    "The toaster is already on fire. "
    "He sprints for the bus and drops the toast. "
    "The dog wins breakfast without even trying. "
    "At the office the projector is broken again. "
    "Nobody is surprised."
)


def _request(
    text: str,
    *,
    pacing: ShotPacing | None,
    joke_annotations: list[dict[str, object]] | None = None,
) -> StoryboardProviderRequest:
    from services.storyboard.boundaries import approved_boundaries, word_boundaries
    from tests.storyboard_fixtures import word_timings

    timings = word_timings(text)
    duration = segment_duration_us(text)
    bounds = word_boundaries(timings, duration)
    approved = approved_boundaries(bounds, text=text, joke_annotations=joke_annotations or [])
    profile = pacing_profile(pacing)
    return StoryboardProviderRequest(
        idempotency_key=f"pacing:{pacing}",
        project_id=uuid4(),
        episode_model_id=uuid4(),
        episode_model_hash="a" * 64,
        script_id=uuid4(),
        script_version=1,
        script_segment_id=uuid4(),
        segment_sequence=0,
        narration_run_id=uuid4(),
        narration_segment_id=uuid4(),
        narration_asset_id=uuid4(),
        measured_duration_us=duration,
        narration_text=text,
        word_timings=bounds,
        approved_boundaries=approved,
        incoming_continuity=ContinuityState(),
        capability=RUNWAY_GEN4_TURBO_PROFILE,
        pacing=profile.guidance() if pacing is not None else None,
        contract_version="storyboard/1.0",
        prompt_version=PROMPT_VERSION,
        attempt_number=1,
    )


def _propose(text: str, pacing: ShotPacing | None, **kwargs):
    return asyncio.run(FakeStoryboardDirector().propose(_request(text, pacing=pacing, **kwargs)))


# -- presets -------------------------------------------------------------------


def test_presets_carry_the_documented_ranges_and_bounded_material() -> None:
    relaxed, normal, fast = (
        PACING_PROFILES[p] for p in (ShotPacing.RELAXED, ShotPacing.NORMAL, ShotPacing.FAST)
    )
    assert (relaxed.target_min_us, relaxed.target_max_us) == (6 * SECOND, 10 * SECOND)
    assert (normal.target_min_us, normal.target_max_us) == (4 * SECOND, 7 * SECOND)
    assert (fast.target_min_us, fast.target_max_us) == (2_500_000, 4 * SECOND)
    assert relaxed.hard_max_us <= RUNWAY_GEN4_TURBO_PROFILE.max_generation_duration_us
    for profile in (relaxed, normal, fast):
        assert isinstance(profile, PacingProfile)
        assert profile.material()["preset"] == profile.preset.value
        guidance = profile.guidance()
        assert guidance.target_min_duration_us <= guidance.target_max_duration_us
        assert guidance.hard_max_duration_us >= guidance.target_max_duration_us
    assert pacing_profile(None) is normal
    assert pacing_profile("fast") is fast


def test_normal_pacing_reproduces_the_retimers_original_bounds() -> None:
    config = retimer_config_for(pacing_profile(ShotPacing.NORMAL))
    assert config.min_shot_duration_us == RetimerConfig().min_shot_duration_us
    assert config.max_shot_duration_us == RetimerConfig().max_shot_duration_us
    assert config.pacing_preset == "normal"
    assert (
        retimer_config_for(pacing_profile(ShotPacing.RELAXED)).max_shot_duration_us == 10 * SECOND
    )
    assert retimer_config_for(pacing_profile(ShotPacing.FAST)).max_shot_duration_us == 5 * SECOND


# -- the director follows the preference at semantic boundaries ---------------------


def _durations(result) -> list[int]:
    return [proposal.desired_duration_us for proposal in result.proposals]


def test_relaxed_normal_and_fast_pacing_guide_the_shot_count() -> None:
    relaxed = _propose(NARRATION, ShotPacing.RELAXED)
    normal = _propose(NARRATION, ShotPacing.NORMAL)
    fast = _propose(NARRATION, ShotPacing.FAST)
    assert len(relaxed.proposals) <= len(normal.proposals) <= len(fast.proposals)
    assert len(relaxed.proposals) < len(fast.proposals)
    assert sum(_durations(relaxed)) == sum(_durations(normal)) == segment_duration_us(NARRATION)
    # The preference shows in the typical shot, not in a mechanical quota.
    mean = lambda result: sum(_durations(result)) / len(result.proposals)  # noqa: E731
    assert mean(relaxed) > mean(normal) > mean(fast)
    assert mean(fast) <= pacing_profile(ShotPacing.FAST).hard_max_us
    for result, preset in (
        (relaxed, ShotPacing.RELAXED),
        (normal, ShotPacing.NORMAL),
        (fast, ShotPacing.FAST),
    ):
        profile = pacing_profile(preset)
        # Every interior shot is inside or below the target range; nothing is
        # stretched past the hard maximum to reach a quota.
        for duration in _durations(result)[:-1]:
            assert duration <= profile.hard_max_us
        assert result.redacted_response_metadata["pacing_preset"] == preset.value


def test_cuts_land_only_on_approved_semantic_boundaries() -> None:
    result = _propose(NARRATION, ShotPacing.NORMAL)
    request = _request(NARRATION, pacing=ShotPacing.NORMAL)
    approved = {boundary.word_index for boundary in request.approved_boundaries}
    interior_ends = [proposal.word_end_index - 1 for proposal in result.proposals[:-1]]
    assert interior_ends
    assert all(end in approved for end in interior_ends)
    # The final shot always ends at the final word.
    assert result.proposals[-1].word_end_index == len(request.word_timings)


def test_no_unnecessary_cut_when_a_segment_already_fits_the_range() -> None:
    short = "Our hero wakes up late, again, and the toaster is already on fire."
    result = _propose(short, ShotPacing.NORMAL)
    assert len(result.proposals) == 1
    assert result.proposals[0].desired_duration_us == segment_duration_us(short)


def test_punchline_and_reaction_shots_may_run_short() -> None:
    # The setup ends at "fire." (7 s); the reaction "Nobody is surprised." is
    # the 1.5 s punchline that closes the segment.
    text = NARRATION
    setup_end = text.index("fire.") + len("fire.")
    punchline_start = text.index("Nobody")
    joke = [
        {
            "setup_span": {"start": 0, "end": setup_end},
            "punchline_span": {"start": punchline_start, "end": len(text)},
        }
    ]
    result = _propose(text, ShotPacing.RELAXED, joke_annotations=joke)
    profile = pacing_profile(ShotPacing.RELAXED)
    durations = _durations(result)
    # The trailing reaction shot is shorter than the relaxed target minimum, and
    # was kept as its own shot rather than merged into the setup.
    assert durations[-1] < profile.target_min_us
    assert durations[-1] >= profile.min_punchline_us
    assert result.proposals[-1].action.beat_intent in {"react", "punchline", "continue"}


def test_the_opening_establishing_shot_is_a_hero_shot() -> None:
    result = _propose(NARRATION, ShotPacing.NORMAL)
    first = result.proposals[0]
    assert first.action.beat_intent == "establish"
    assert first.importance == HERO_IMPORTANCE >= 0.8
    assert all(proposal.importance < 0.8 for proposal in result.proposals[1:])


def test_a_request_without_pacing_guidance_still_plans_at_normal_pacing() -> None:
    legacy = _propose(NARRATION, None)
    normal = _propose(NARRATION, ShotPacing.NORMAL)
    assert _durations(legacy) == _durations(normal)


# -- the retimer remains the authority -----------------------------------------------


def _timings(count: int) -> list[NarrationBoundary]:
    return [
        NarrationBoundary(word_index=index, offset_us=(index + 1) * 500_000, kind="word")
        for index in range(count)
    ]


@pytest.mark.parametrize("preset", list(ShotPacing))
def test_the_retimer_covers_narration_exactly_under_every_preset(preset: ShotPacing) -> None:
    request = _request(NARRATION, pacing=preset)
    proposals = _propose(NARRATION, preset).proposals
    timing = retime_segment(
        segment_sequence=0,
        narration_duration_us=request.measured_duration_us,
        word_timings=request.word_timings,
        approved_boundaries=request.approved_boundaries,
        proposals=proposals,
        capability=RUNWAY_GEN4_TURBO_PROFILE,
        config=retimer_config_for(pacing_profile(preset)),
    )
    cursor = 0
    for shot in timing.shots:
        assert shot.start_us == cursor
        assert shot.usable_duration_us > 0
        # Never a duration the provider cannot generate, always deterministic trimming.
        assert RUNWAY_GEN4_TURBO_PROFILE.is_supported_duration(
            shot.requested_generation_duration_us
        )
        assert shot.requested_generation_duration_us >= shot.usable_duration_us
        assert shot.trim_start_us + shot.trim_end_us == (
            shot.requested_generation_duration_us - shot.usable_duration_us
        )
        cursor = shot.end_us
    assert cursor == request.measured_duration_us
    again = retime_segment(
        segment_sequence=0,
        narration_duration_us=request.measured_duration_us,
        word_timings=request.word_timings,
        approved_boundaries=request.approved_boundaries,
        proposals=proposals,
        capability=RUNWAY_GEN4_TURBO_PROFILE,
        config=retimer_config_for(pacing_profile(preset)),
    )
    assert again.shots == timing.shots


def test_narration_is_never_stretched_to_meet_a_pacing_target() -> None:
    # A director wanting 9 s relaxed shots against 4 s of measured narration.
    from tests.test_storyboard_retimer import proposal

    timing = retime_segment(
        segment_sequence=0,
        narration_duration_us=4 * SECOND,
        word_timings=_timings(8),
        approved_boundaries=[],
        proposals=[proposal(0, 0, 8, 9 * SECOND)],
        capability=RUNWAY_GEN4_TURBO_PROFILE,
        config=retimer_config_for(pacing_profile(ShotPacing.RELAXED)),
    )
    assert sum(shot.usable_duration_us for shot in timing.shots) == 4 * SECOND
    assert timing.shots[-1].end_us == 4 * SECOND
    assert any(item.kind == "final_end_snap" for item in timing.adjustments)


# -- the pipeline binds the preset into the storyboard identity -------------------------


def test_changing_the_pacing_preset_changes_the_storyboard_identity(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, texts=(NARRATION,))
    normal = run_pipeline(fixture, key="normal")
    normal_run = fixture.session.get(StoryboardRun, normal.storyboard_run_id)
    fixture.project.settings = with_generation_settings(
        fixture.project.settings, ProjectGenerationSettings(shot_pacing=ShotPacing.FAST)
    )
    fixture.session.commit()
    fast = run_pipeline(fixture, key="fast")
    fast_run = fixture.session.get(StoryboardRun, fast.storyboard_run_id)
    assert normal_run.input_hash != fast_run.input_hash
    assert normal_run.parameters["shot_pacing"] == "normal"
    assert fast_run.parameters["shot_pacing"] == "fast"
    assert fast_run.parameters["retimer_config"]["pacing_preset"] == "fast"
    assert fast.shot_count > normal.shot_count
    storyboard = load_storyboard(fixture, fast)
    manifest = load_manifest(fixture, fast)
    assert manifest.total_usable_duration_us == segment_duration_us(NARRATION)
    for shot in storyboard.shots:
        assert shot.provenance["shot_pacing"] == "fast"
        assert "importance" in shot.provenance
        assert isinstance(shot.provenance["hero_shot"], bool)
    assert storyboard.shots[0].provenance["hero_shot"] is True
    # Reusing the normal key with the fast preset is refused, never silently reused.
    with pytest.raises(Exception, match="idempotency"):
        run_pipeline(fixture, key="normal")


def test_generation_policy_identity_changes_with_quality_or_pacing() -> None:
    profile = RUNWAY_GEN4_TURBO_PROFILE
    base = ProjectGenerationSettings()
    identity = generation_policy_identity(
        base,
        capability_profile_id=profile.capability_profile_id,
        capability_hash=profile.capability_hash,
    )
    assert identity == generation_policy_identity(
        base,
        capability_profile_id=profile.capability_profile_id,
        capability_hash=profile.capability_hash,
    )
    assert len(identity) <= 160
    for changed in (
        ProjectGenerationSettings(generation_quality=GenerationQuality.PREMIUM),
        ProjectGenerationSettings(shot_pacing=ShotPacing.RELAXED),
        ProjectGenerationSettings(premium_fallback_allowed=True),
    ):
        assert (
            generation_policy_identity(
                changed,
                capability_profile_id=profile.capability_profile_id,
                capability_hash=profile.capability_hash,
            )
            != identity
        )
    assert (
        generation_policy_identity(
            base, capability_profile_id="runway-gen4.5", capability_hash="b" * 64
        )
        != identity
    )


def test_legacy_projects_resolve_to_economy_and_new_projects_to_balanced() -> None:
    legacy = resolve_generation_settings({})
    assert legacy.generation_quality is GenerationQuality.ECONOMY
    assert legacy.shot_pacing is ShotPacing.NORMAL
    assert legacy.origin.value == "legacy_default"
    assert resolve_generation_settings(None) == legacy
    hero_profile = resolve_generation_settings({"quality_profile": "hero"})
    assert hero_profile.generation_quality is GenerationQuality.BALANCED
    explicit = resolve_generation_settings(
        with_generation_settings({"storyboard": {}}, ProjectGenerationSettings())
    )
    assert explicit.generation_quality is GenerationQuality.BALANCED
    assert explicit.origin.value == "explicit"
    with pytest.raises(ValueError, match="invalid"):
        resolve_generation_settings({"generation": {"generation_quality": "ultra"}})
