from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vidgen.contracts import EpisodeAnalysis, ShotDefinition

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def test_episode_analysis_round_trip() -> None:
    payload = json.loads((FIXTURES / "episode_analysis.valid.json").read_text())
    contract = EpisodeAnalysis.model_validate(payload)
    assert EpisodeAnalysis.model_validate_json(contract.model_dump_json()) == contract


def test_invalid_contract_is_rejected() -> None:
    payload = json.loads((FIXTURES / "episode_analysis.invalid.json").read_text())
    with pytest.raises(ValidationError):
        EpisodeAnalysis.model_validate(payload)


def test_shot_rejects_zero_duration() -> None:
    with pytest.raises(ValidationError):
        ShotDefinition.model_validate(
            {
                "shot_id": "00000000-0000-0000-0000-000000000001",
                "segment_id": "00000000-0000-0000-0000-000000000002",
                "sequence": 0,
                "duration_seconds": 0,
                "location_id": "00000000-0000-0000-0000-000000000003",
                "action": "stands still",
                "composition": "medium shot",
            }
        )
