"""Reproducible T09 source-frame selection for T19."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from services.continuity.candidate_scoring import score_candidate
from vidgen.contracts.continuity import (
    CharacterReferenceCandidate,
    LocationReferenceCandidate,
)

Candidate = CharacterReferenceCandidate | LocationReferenceCandidate


def select_candidates(candidates: Sequence[Any], *, maximum: int = 8) -> list[Any]:
    """Remove exact hashes and rank candidates with deterministic tie breaking."""
    if maximum < 1:
        raise ValueError("maximum must be positive")
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        current = unique.get(candidate.sha256)
        if current is None or (
            score_candidate(candidate.scores),
            -candidate.source_timestamp_ms,
            str(candidate.asset_id),
        ) > (score_candidate(current.scores), -current.source_timestamp_ms, str(current.asset_id)):
            unique[candidate.sha256] = candidate
    return sorted(
        unique.values(),
        key=lambda value: (
            -score_candidate(value.scores),
            value.source_timestamp_ms,
            str(value.asset_id),
        ),
    )[:maximum]
