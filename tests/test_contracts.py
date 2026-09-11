from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from vidgen.contracts import EpisodeAnalysis, StoryboardShot
from vidgen.contracts.storyboard import (
    CharacterAppearanceState,
    ContinuityState,
    SubjectPosition,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def test_episode_analysis_round_trip() -> None:
    payload = json.loads((FIXTURES / "episode_analysis.valid.json").read_text())
    contract = EpisodeAnalysis.model_validate(payload)
    assert EpisodeAnalysis.model_validate_json(contract.model_dump_json()) == contract


def test_invalid_contract_is_rejected() -> None:
    payload = json.loads((FIXTURES / "episode_analysis.invalid.json").read_text())
    with pytest.raises(ValidationError):
        EpisodeAnalysis.model_validate(payload)


def test_continuity_reports_undeclared_characters_instead_of_rejecting_them() -> None:
    """The Director's commonest continuity slip is a finding, not a schema error.

    ``present_character_ids`` is authoritative, so the state is kept exactly as
    proposed and the defect is surfaced for a targeted repair rather than
    raising a ValidationError that would abort the whole storyboard run.
    """
    positioned = UUID("1" * 32)
    dressed = UUID("2" * 32)
    declared = UUID("3" * 32)
    state = ContinuityState.model_validate(
        {
            "present_character_ids": [str(declared)],
            "subject_positions": [
                {"character_id": str(positioned), "screen_position": "left"},
                {"character_id": str(declared), "screen_position": "center"},
            ],
            "character_appearance_states": [
                {"character_id": str(dressed), "appearance_state_id": "soaked"}
            ],
        }
    )
    assert state.present_character_ids == [declared]
    problems = state.undeclared_character_references()
    assert [(problem.field, problem.character_id) for problem in problems] == [
        ("character_appearance_states", dressed),
        ("subject_positions", positioned),
    ]
    assert "present_character_ids" in problems[0].message


def test_continuity_with_a_fully_declared_cast_reports_nothing() -> None:
    character_id = UUID("4" * 32)
    state = ContinuityState(
        present_character_ids=[character_id],
        subject_positions=[SubjectPosition(character_id=character_id, screen_position="center")],
        character_appearance_states=[
            CharacterAppearanceState(character_id=character_id, appearance_state_id="default")
        ],
    )
    assert state.undeclared_character_references() == []


def test_shot_rejects_zero_duration() -> None:
    with pytest.raises(ValidationError):
        StoryboardShot.model_validate(_shot(usable_duration_us=0, end_us=0))


def test_shot_rejects_timing_that_does_not_match_its_interval() -> None:
    with pytest.raises(ValidationError, match="usable_duration_us"):
        StoryboardShot.model_validate(_shot(usable_duration_us=999))


def test_shot_rejects_trim_that_does_not_account_for_the_generated_duration() -> None:
    with pytest.raises(ValidationError, match="trim values"):
        StoryboardShot.model_validate(
            _shot(requested_generation_duration_us=3_000_000, trim_end_us=0)
        )


def _shot(**overrides: object) -> dict[str, object]:
    identifier = "00000000-0000-0000-0000-0000000000"
    plan = {
        "camera": {
            "framing": "medium",
            "angle": "eye_level",
            "movement": "static",
            "movement_intensity": "none",
        },
        "action": {"subject_action": "stands still", "beat_intent": "continue"},
        "transition_in": {"kind": "cut"},
        "transition_out": {"kind": "cut"},
        "incoming_continuity": {},
        "expected_outgoing_continuity": {},
    }
    payload: dict[str, object] = {
        "shot_id": f"{identifier}01",
        "storyboard_run_id": f"{identifier}02",
        "segment_id": f"{identifier}03",
        "global_sequence": 0,
        "segment_sequence": 0,
        "script_segment_id": f"{identifier}04",
        "narration_segment_id": f"{identifier}05",
        "start_us": 0,
        "end_us": 2_000_000,
        "global_start_us": 0,
        "global_end_us": 2_000_000,
        "usable_duration_us": 2_000_000,
        "requested_generation_duration_us": 2_000_000,
        "trim_start_us": 0,
        "trim_end_us": 0,
        "word_start_index": 0,
        "word_end_index": 4,
        "visual_objective": "show the fire",
        "capability_profile_id": "runway-gen4-turbo",
        "capability_hash": "a" * 64,
        **plan,
    }
    payload.update(overrides)
    return payload
