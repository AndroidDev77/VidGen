from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.continuity.bible_builder import build_character_bible, build_location_bible
from services.continuity.bindings import compact_references, make_bundle
from services.continuity.candidate_scoring import score_candidate
from services.continuity.invalidation import affected_shots
from services.continuity.reference_generator import DeterministicFakeReferenceGenerator
from services.continuity.reference_selector import select_candidates
from services.continuity.state_resolver import resolve_character_state, resolve_location_state
from services.continuity.t14_integration import bundle_references, continuity_prompt_identity
from vidgen.contracts.continuity import (
    CandidateScores,
    CharacterAppearanceState,
    CharacterReferenceCandidate,
    EvidenceLink,
    Interval,
    LocationEnvironmentState,
    ReferenceBundleItem,
    ReferenceGenerationRequest,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64


def scores(**updates: float) -> CandidateScores:
    values = dict(
        evidence=0.9,
        visibility=0.9,
        sharpness=0.9,
        exposure=0.9,
        obstruction=0.1,
        state_relevance=0.8,
        diversity=0.7,
    )
    values.update(updates)
    return CandidateScores(**values)


def candidate(
    *, digest: str = HASH, timestamp: int = 100, **updates: float
) -> CharacterReferenceCandidate:
    component = scores(**updates)
    return CharacterReferenceCandidate(
        asset_id=uuid4(),
        source_scene_id=uuid4(),
        source_timestamp_ms=timestamp,
        scores=component,
        total_score=score_candidate(component),
        character_id=uuid4(),
        width=1920,
        height=1080,
        sha256=digest,
        selector_version="continuity-candidate/1.0",
        created_at=NOW,
    )


def test_candidate_scoring_penalizes_occlusion_and_is_deterministic() -> None:
    clear = scores(obstruction=0.0, sharpness=1.0)
    blocked = scores(obstruction=1.0, sharpness=0.0)
    assert score_candidate(clear) == score_candidate(clear)
    assert score_candidate(clear) > score_candidate(blocked)


def test_candidate_selection_deduplicates_hashes_and_orders_quality() -> None:
    weak = candidate(timestamp=1, sharpness=0.1)
    duplicate_better = candidate(timestamp=2, sharpness=1.0)
    best = candidate(digest="b" * 64, timestamp=3, sharpness=1.0, obstruction=0.0)
    selected = select_candidates([weak, duplicate_better, best])
    assert [item.sha256 for item in selected] == [best.sha256, duplicate_better.sha256]


def test_bibles_separate_temporary_traits_and_preserve_anonymous_identity() -> None:
    character_id, location_id = uuid4(), uuid4()
    evidence = [EvidenceLink(evidence_id=uuid4(), source_timestamp_ms=10)]
    bible = build_character_bible(
        character_id=character_id,
        display_name="Invented name",
        aliases=["Alias"],
        anonymous_speaker_label="Speaker 3",
        observations={
            "hair": ["black"],
            "wardrobe": ["coat"],
            "facial_structure": ["round", "angular"],
        },
        evidence=evidence,
        confidence=0.8,
    )
    assert bible.display_name == "Speaker 3" and bible.aliases == []
    assert "wardrobe" not in bible.stable_traits
    assert bible.stable_traits["facial_structure"] is None and bible.ambiguities
    location = build_location_bible(
        location_id=location_id,
        display_name="Kitchen",
        location_type="interior",
        observations={"materials": ["tile"], "time_of_day": ["night"]},
        evidence=evidence,
        confidence=0.9,
    )
    assert location.stable_traits == {"materials": "tile"}


def test_interval_resolution_carries_persistent_state_without_future_leakage() -> None:
    before = CharacterAppearanceState(
        interval=Interval(start_sequence=2), wardrobe=["blue coat"], confidence=1
    )
    injury = CharacterAppearanceState(
        interval=Interval(start_sequence=6, end_sequence=8), injuries=["bandaged arm"], confidence=1
    )
    assert resolve_character_state([before, injury], 1) is None
    assert resolve_character_state([before, injury], 5) == before
    combined = resolve_character_state([before, injury], 7)
    assert combined is not None
    assert combined.wardrobe == ["blue coat"] and combined.injuries == ["bandaged arm"]
    assert resolve_character_state([before, injury], 9) == before
    night = LocationEnvironmentState(
        interval=Interval(start_sequence=5), time_of_day="night", confidence=1
    )
    assert resolve_location_state([night], 4) is None
    assert resolve_location_state([night], 5) == night


def test_fake_generation_is_idempotent_and_material_inputs_change_identity() -> None:
    request = ReferenceGenerationRequest(
        project_id=uuid4(),
        identity_version_id=uuid4(),
        entity_kind="character",
        ordered_source_asset_ids=[uuid4()],
        provider="fake",
        model="fake-v1",
        idempotency_key="same",
    )
    provider = DeterministicFakeReferenceGenerator(completed={})
    first = provider.generate(request, [HASH])
    second = provider.generate(request, [HASH])
    changed = provider.generate(request, ["b" * 64])
    assert not first.reused and second.reused
    assert first.asset_id == second.asset_id
    assert changed.reference_identity != first.reference_identity
    assert len(provider.completed) == 2


def test_required_compaction_bundle_hash_t14_and_exact_invalidation() -> None:
    project_id, storyboard_id, shot_id, character_id, location_id = [uuid4() for _ in range(5)]
    required = ReferenceBundleItem(
        asset_id=uuid4(),
        sha256=HASH,
        role="character_identity",
        entity_id=character_id,
        required=True,
        priority=0,
    )
    optional = ReferenceBundleItem(
        asset_id=uuid4(),
        sha256="b" * 64,
        role="location_state",
        entity_id=location_id,
        required=False,
        priority=9,
    )
    kept, omitted = compact_references([optional, required], 1)
    assert kept == [required] and str(optional.asset_id) in omitted[0]
    with pytest.raises(ValueError, match="cannot fit"):
        compact_references([required], 0)
    bundle = make_bundle(
        project_id=project_id,
        storyboard_run_id=storyboard_id,
        shot_id=shot_id,
        shot_sequence=0,
        references=[required, optional],
        provider_reference_limit=1,
        character_identity_version_ids=[uuid4()],
    )
    assert bundle_references(bundle)[0].asset_id == required.asset_id
    assert continuity_prompt_identity(bundle)["reference_bundle_hash"] == bundle.bundle_hash
    untouched = uuid4()
    assert affected_shots({shot_id: {character_id}, untouched: {location_id}}, {character_id}) == [
        shot_id
    ]
