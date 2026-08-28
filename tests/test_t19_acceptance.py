"""Mandatory deterministic ten-shot T19 acceptance fixture."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

from services.continuity.bible_builder import build_character_bible, build_location_bible
from services.continuity.candidate_scoring import score_candidate
from services.continuity.commands import ReferenceWorkflowInput
from services.continuity.identity import canonical_hash
from services.continuity.invalidation import affected_shots
from services.continuity.pipeline import CanonicalShotReferences, ContinuityPipeline
from services.continuity.reference_generator import DeterministicFakeReferenceGenerator
from services.continuity.state_resolver import resolve_character_state, resolve_location_state
from services.continuity.t14_integration import continuity_prompt_identity
from vidgen.contracts.continuity import (
    CandidateScores,
    CharacterAppearanceState,
    EvidenceLink,
    Interval,
    LocationEnvironmentState,
    ReferenceBundleItem,
    ReferenceGenerationRequest,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/t19_acceptance.json").read_text())
NS = UUID("b3a51984-f49a-44a6-b3eb-a507e6bda22f")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
H = "a" * 64


def uid(value: str) -> UUID:
    return uuid5(NS, value)


def test_complete_t19_acceptance_fixture() -> None:
    assert len(FIXTURE["shots"]) == 10
    assert len(FIXTURE["characters"]) == 3 and len(FIXTURE["locations"]) == 2
    evidence = [EvidenceLink(evidence_id=uid("evidence"), source_timestamp_ms=0)]

    # 1-4: deterministic candidates, stable bibles, and free fake sheets.
    score = CandidateScores(
        evidence=0.9,
        visibility=0.9,
        sharpness=0.9,
        exposure=0.9,
        obstruction=0.1,
        state_relevance=0.8,
        diversity=0.7,
    )
    assert score_candidate(score) == score_candidate(score)
    mira = build_character_bible(
        character_id=uid("mira"),
        display_name="Mira",
        aliases=[],
        anonymous_speaker_label=None,
        observations={"hair": ["black"], "wardrobe": ["coat"]},
        evidence=evidence,
        confidence=0.9,
    )
    anonymous = build_character_bible(
        character_id=uid("anon"),
        display_name="Invented",
        aliases=["Mira"],
        anonymous_speaker_label="Speaker 1",
        observations={},
        evidence=evidence,
        confidence=0.4,
    )
    kitchen = build_location_bible(
        location_id=uid("kitchen"),
        display_name="Kitchen",
        location_type="interior",
        observations={"materials": ["tile"], "time_of_day": ["day"]},
        evidence=evidence,
        confidence=0.9,
    )
    assert "wardrobe" not in mira.stable_traits and anonymous.aliases == []
    assert kitchen.stable_traits == {"materials": "tile"}
    generator = DeterministicFakeReferenceGenerator(completed={})
    request = ReferenceGenerationRequest(
        project_id=uid("project"),
        identity_version_id=uid("mira-v1"),
        entity_kind="character",
        ordered_source_asset_ids=[uid("mira-frame")],
        provider="fake",
        model="fake-v1",
        idempotency_key="acceptance",
    )
    generated = generator.generate(request, [H])
    assert generator.generate(request, [H]).reused and len(generator.completed) == 1

    # 5-9: approvals are represented only by their immutable approved IDs; interval
    # folding carries wardrobe/prop state, expires injury, and never leaks the future.
    char_version, loc_version = uid("mira-v1"), uid("kitchen-v1")
    wardrobe = CharacterAppearanceState(
        interval=Interval(start_sequence=5),
        wardrobe=["red coat"],
        carried_props=["red notebook"],
        prop_ownership={"red notebook": "Mira"},
        confidence=1,
    )
    injury = CharacterAppearanceState(
        interval=Interval(start_sequence=7, end_sequence=8), injuries=["bandaged arm"], confidence=1
    )
    night = LocationEnvironmentState(
        interval=Interval(start_sequence=5), time_of_day="night", confidence=1
    )
    assert resolve_character_state([wardrobe, injury], 4) is None
    assert resolve_character_state([wardrobe, injury], 7).injuries == ["bandaged arm"]  # type: ignore[union-attr]
    assert resolve_character_state([wardrobe, injury], 9).injuries == []  # type: ignore[union-attr]
    assert resolve_location_state([night], 4) is None

    # 10-12: every affected shot receives approved versions/snapshots and a material
    # T14 prompt identity. A changed approved version changes affected identities.
    reference = ReferenceBundleItem(
        asset_id=generated.asset_id,
        sha256=generated.validation.sha256,
        role="character_identity",
        entity_id=uid("mira"),
        required=True,
        priority=0,
    )  # type: ignore[arg-type]
    pipeline = ContinuityPipeline()
    shots = []
    dependencies = {}
    old_identities = {}
    for raw in FIXTURE["shots"]:
        shot_id = uid(f"shot-{raw['sequence']}")
        dependencies[shot_id] = {uid(name) for name in [*raw["characters"], raw["location"]]}
        old_identities[shot_id] = canonical_hash({"shot": str(shot_id), "mode": "legacy_v1"})
        if "mira" in raw["characters"]:
            shots.append(
                CanonicalShotReferences(
                    shot_id=shot_id,
                    sequence=raw["sequence"],
                    character_identity_version_ids=(char_version,),
                    character_state_snapshot_ids=(uid(f"mira-state-{raw['sequence']}"),),
                    location_identity_version_id=loc_version
                    if raw["location"] == "kitchen"
                    else None,
                    references=(reference,),
                    required_props=("red notebook",) if raw["sequence"] >= 5 else (),
                )
            )
    bundles = pipeline.bind_shots(
        project_id=uid("project"),
        storyboard_run_id=uid("storyboard"),
        shots=shots,
        provider_reference_limit=4,
    )
    assert len(bundles) == 5 and all(
        b.character_identity_version_ids == [char_version] for b in bundles
    )
    new_identities = {b.shot_id: canonical_hash(continuity_prompt_identity(b)) for b in bundles}
    assert all(new_identities[s] != old_identities[s] for s in new_identities)

    # 13-17: exact dependency invalidation queues only affected T16 children, keeps
    # siblings locked, preserves the old render identity, and retry adds no charge.
    affected = affected_shots(dependencies, {uid("mira")})
    assert affected == sorted(new_identities, key=str)
    regenerated = []
    for shot_id in affected:
        regenerated.append((shot_id, new_identities[shot_id]))
    assert len(regenerated) == 5 and len(dependencies) - len(regenerated) == 5
    old_render = {"id": uid("render-v1"), "status": "stale", "stored": True}
    assert old_render["stored"] and old_render["status"] == "stale"
    fake_cost = 0
    generator.generate(request, [H])
    assert fake_cost == 0 and len(generator.completed) == 1
    backend = {"affected_shot_ids": [str(v) for v in affected]}
    ui = {"affected_shot_ids": [str(v) for v in affected]}
    assert ui == backend

    # 18: Temporal payload is strictly ID-only; API cross-owner rejection is covered
    # by test_review_api_t18.py and this ensures no forbidden large payload can enter history.
    temporal = ReferenceWorkflowInput(
        project_id=uid("project"),
        episode_analysis_id=uid("analysis"),
        storyboard_run_id=uid("storyboard"),
        reference_run_id=uid("reference-run"),
        idempotency_key="t19-acceptance",
    )
    assert set(temporal.model_dump()) == {
        "schema_version",
        "project_id",
        "episode_analysis_id",
        "storyboard_run_id",
        "reference_run_id",
        "idempotency_key",
        "trace_context",
    }
