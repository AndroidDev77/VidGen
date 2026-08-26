"""Stable IDs and ordering for validated analysis output."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid5

from vidgen.contracts.episode_analysis import EpisodeAnalysis

ANALYSIS_NAMESPACE = UUID("6932dd53-9d6c-4fd7-985e-bf48338b8bc6")


def stable_id(
    *,
    evidence_hash: str,
    kind: str,
    key: str,
    prompt_version: str,
    contract_version: str,
    provider_configuration_version: str,
) -> UUID:
    canonical_key = ":".join(
        (
            evidence_hash,
            prompt_version,
            contract_version,
            provider_configuration_version,
            kind,
            key.strip().casefold(),
        )
    )
    return uuid5(ANALYSIS_NAMESPACE, canonical_key)


def canonicalize(analysis: EpisodeAnalysis) -> EpisodeAnalysis:
    """Sort unordered collections without deriving IDs from list positions."""
    data = analysis.model_copy(deep=True)
    data.characters.sort(key=lambda item: (item.canonical_name.casefold(), str(item.character_id)))
    data.locations.sort(key=lambda item: (item.canonical_name.casefold(), str(item.location_id)))
    data.scenes.sort(key=lambda item: (item.sequence, item.source_start_ms, str(item.scene_id)))
    data.state_events.sort(key=lambda item: (item.sequence, str(item.state_event_id)))
    data.relationships.sort(key=lambda item: str(item.relationship_id))
    data.plot_beats.sort(key=lambda item: (item.sequence, str(item.plot_beat_id)))
    data.beat_dependencies.sort(
        key=lambda item: (str(item.cause_beat_id), str(item.effect_beat_id))
    )
    data.unresolved_ambiguities.sort(key=lambda item: str(item.ambiguity_id))
    return data


def canonical_hash(analysis: EpisodeAnalysis) -> str:
    payload = json.dumps(
        canonicalize(analysis).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()
