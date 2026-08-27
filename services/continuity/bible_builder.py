"""Deterministic evidence-bounded identity-bible construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from vidgen.contracts.continuity import (
    CharacterIdentityBible,
    ContinuityAmbiguity,
    EvidenceLink,
    LocationIdentityBible,
)

TEMPORARY_CHARACTER_FIELDS = {
    "wardrobe",
    "hairstyle_change",
    "injury",
    "dirt",
    "damage",
    "carried_prop",
    "emotion",
    "action",
    "disguise",
}
TEMPORARY_LOCATION_FIELDS = {
    "time_of_day",
    "weather",
    "lighting",
    "damage",
    "crowd",
    "prop_placement",
    "door_state",
    "window_state",
    "season",
    "cleanliness",
    "hazard",
    "decoration",
}


def _stable_traits(
    observations: Mapping[str, Sequence[str]], temporary: set[str]
) -> tuple[dict[str, str | list[str] | None], list[ContinuityAmbiguity]]:
    stable: dict[str, str | list[str] | None] = {}
    ambiguities: list[ContinuityAmbiguity] = []
    for field in sorted(observations):
        if field in temporary:
            continue
        values = sorted(set(observations[field]))
        if len(values) == 1:
            stable[field] = values[0]
        elif values:
            stable[field] = None
            ambiguities.append(ContinuityAmbiguity(field=field, alternatives=values))
    return stable, ambiguities


def build_character_bible(
    *,
    character_id: UUID,
    display_name: str,
    aliases: Sequence[str],
    observations: Mapping[str, Sequence[str]],
    evidence: Sequence[EvidenceLink],
    confidence: float,
    anonymous_speaker_label: str | None = None,
) -> CharacterIdentityBible:
    stable, ambiguities = _stable_traits(observations, TEMPORARY_CHARACTER_FIELDS)
    # Anonymous identities remain anonymous; aliases are evidence inputs, not identity promotion.
    canonical_name = anonymous_speaker_label or display_name
    return CharacterIdentityBible(
        character_id=character_id,
        display_name=canonical_name,
        anonymous_speaker_label=anonymous_speaker_label,
        aliases=sorted(set(aliases)) if anonymous_speaker_label is None else [],
        stable_traits=stable,
        evidence=list(evidence),
        confidence=confidence,
        ambiguities=ambiguities,
    )


def build_location_bible(
    *,
    location_id: UUID,
    display_name: str,
    location_type: str | None,
    observations: Mapping[str, Sequence[str]],
    evidence: Sequence[EvidenceLink],
    confidence: float,
) -> LocationIdentityBible:
    stable, ambiguities = _stable_traits(observations, TEMPORARY_LOCATION_FIELDS)
    return LocationIdentityBible(
        location_id=location_id,
        display_name=display_name,
        location_type=location_type,
        stable_traits=stable,
        evidence=list(evidence),
        confidence=confidence,
        ambiguities=ambiguities,
    )
