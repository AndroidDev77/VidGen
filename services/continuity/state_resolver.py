"""Interval folding without future-state leakage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vidgen.contracts.continuity import CharacterAppearanceState, LocationEnvironmentState

RESOLVER_VERSION = "continuity-state/1.0"


def _resolve(states: Sequence[Any], shot_sequence: int) -> Any | None:
    eligible = [
        state
        for state in states
        if state.interval.start_sequence <= shot_sequence
        and (state.interval.end_sequence is None or shot_sequence <= state.interval.end_sequence)
    ]
    if not eligible:
        return None
    ordered = sorted(
        eligible,
        key=lambda state: (state.interval.start_sequence, state.model_dump_json()),
    )
    merged = ordered[0]
    for state in ordered[1:]:
        updates: dict[str, Any] = {}
        for field, value in state.model_dump().items():
            if field in {"schema_version", "interval", "confidence"}:
                continue
            current = getattr(merged, field)
            if isinstance(value, list):
                if value:
                    updates[field] = list(dict.fromkeys([*current, *value]))
            elif isinstance(value, dict):
                if value:
                    updates[field] = {**current, **value}
            elif value is not None:
                updates[field] = value
        updates["confidence"] = min(merged.confidence, state.confidence)
        merged = merged.model_copy(update=updates)
    return merged


def resolve_character_state(
    states: Sequence[CharacterAppearanceState], shot_sequence: int
) -> CharacterAppearanceState | None:
    return _resolve(states, shot_sequence)


def resolve_location_state(
    states: Sequence[LocationEnvironmentState], shot_sequence: int
) -> LocationEnvironmentState | None:
    return _resolve(states, shot_sequence)
