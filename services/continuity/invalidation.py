"""Exact dependency-based reference invalidation."""

from __future__ import annotations

from collections.abc import Mapping, Set
from uuid import UUID


def affected_shots(
    shot_dependencies: Mapping[UUID, Set[UUID]], changed_entities: Set[UUID]
) -> list[UUID]:
    """Return only shots whose explicit dependency set intersects the change set."""
    return sorted(
        (
            shot_id
            for shot_id, dependencies in shot_dependencies.items()
            if dependencies & changed_entities
        ),
        key=str,
    )
