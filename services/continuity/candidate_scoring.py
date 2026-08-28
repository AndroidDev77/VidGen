"""Versioned deterministic candidate scoring (no model-derived measurements)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vidgen.contracts.continuity import CandidateScores

SELECTOR_VERSION = "continuity-candidate/1.0"

# Positive quality signals sum to one. Obstruction is an inverse quality signal.
WEIGHTS = {
    "evidence": 0.24,
    "visibility": 0.20,
    "sharpness": 0.16,
    "exposure": 0.10,
    "obstruction": 0.14,
    "state_relevance": 0.10,
    "diversity": 0.06,
}


def score_candidate(scores: CandidateScores) -> float:
    """Return a stable six-decimal quality score in [0, 1]."""
    values = scores.model_dump(exclude={"schema_version"})
    values["obstruction"] = 1.0 - scores.obstruction
    return round(sum(float(values[key]) * weight for key, weight in WEIGHTS.items()), 6)


def rank_candidates(candidates: list[Any], *, score: Callable[[Any], float]) -> list[Any]:
    """Sort by quality and then stable asset identity supplied by candidate objects."""
    return sorted(candidates, key=lambda candidate: (-score(candidate), str(candidate.asset_id)))
