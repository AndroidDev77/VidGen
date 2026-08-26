"""Stable identities, canonical ordering, and content hashing for T13.

Identity is derived, never random: the same inputs always produce the same run,
segment, shot, manifest, provider-request, and repair-attempt identities. That is
what lets an interrupted run resume and a completed run return without new
provider submissions or cost events.
"""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID, uuid5

from vidgen.contracts.storyboard import (
    MICROSECONDS_PER_SECOND,
    StoryboardProviderResult,
    StoryboardShotProposal,
    VisualProviderCapability,
)

STORYBOARD_NAMESPACE = UUID("6b1f7f1a-2f4d-4a53-9f6e-1c2f4b8d7a01")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def seconds_to_us(seconds: float | Decimal | str) -> int:
    """Convert a measured ffprobe duration to exact integer microseconds.

    ``Decimal(str(...))`` reads the decimal literal the probe reported rather
    than the nearest binary double, so the conversion is reproducible.
    """
    quantized = Decimal(str(seconds)).scaleb(6).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(quantized)


def us_to_seconds(microseconds: int) -> Decimal:
    return (Decimal(microseconds) / MICROSECONDS_PER_SECOND).normalize()


def stable_id(kind: str, *parts: object) -> UUID:
    """Derive a deterministic UUID5 from a namespaced, ordered key."""
    return uuid5(STORYBOARD_NAMESPACE, ":".join((kind, *(str(part) for part in parts))))


def capability_material(profile: dict[str, Any]) -> dict[str, Any]:
    """The hashed subset of a capability profile, excluding the hash itself."""
    return {key: value for key, value in profile.items() if key != "capability_hash"}


def capability_profile_hash(profile: dict[str, Any]) -> str:
    return canonical_hash(capability_material(profile))


def capability_hash_of(capability: VisualProviderCapability) -> str:
    return capability_profile_hash(capability.model_dump(mode="json"))


def canonicalize_proposal(proposal: StoryboardShotProposal) -> StoryboardShotProposal:
    """Order every unordered collection inside one proposal."""
    data = proposal.model_copy(deep=True)
    data.character_reference_ids = sorted(set(data.character_reference_ids), key=str)
    data.action.prop_references = sorted(set(data.action.prop_references))
    data.evidence_references = sorted(
        data.evidence_references,
        key=lambda ref: (ref.reference_type, str(ref.reference_id), ref.start_us or 0),
    )
    for state in (data.incoming_continuity, data.expected_outgoing_continuity):
        state.present_character_ids = sorted(set(state.present_character_ids), key=str)
        state.character_appearance_states = sorted(
            state.character_appearance_states, key=lambda item: str(item.character_id)
        )
        state.props = sorted(state.props, key=lambda item: item.prop_id)
        state.subject_positions = sorted(
            state.subject_positions, key=lambda item: str(item.character_id)
        )
        state.environment_conditions = sorted(set(state.environment_conditions))
        state.unresolved_warnings = sorted(
            state.unresolved_warnings, key=lambda note: (note.code, note.message)
        )
    data.warnings = sorted(data.warnings, key=lambda note: (note.code, note.message))
    return data


def canonicalize_provider_result(result: StoryboardProviderResult) -> StoryboardProviderResult:
    """Canonicalize unordered provider output before it is hashed or persisted."""
    data = result.model_copy(deep=True)
    ordered = sorted(data.proposals, key=lambda item: item.proposal_sequence)
    data.proposals = [
        canonicalize_proposal(proposal).model_copy(update={"proposal_sequence": index})
        for index, proposal in enumerate(ordered)
    ]
    data.warnings = sorted(data.warnings, key=lambda note: (note.code, note.message))
    return data


def provider_result_hash(result: StoryboardProviderResult) -> str:
    canonical = canonicalize_provider_result(result).model_dump(mode="json")
    # Provider metadata is deliberately excluded: it is transport, not content.
    for key in ("provider_request_id", "usage", "redacted_response_metadata", "attempt_number"):
        canonical.pop(key, None)
    return canonical_hash(canonical)
