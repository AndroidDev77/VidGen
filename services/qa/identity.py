"""T19 character-identity comparison inputs for T20.

Identity QA always compares a generated asset against the *exact* approved T19
identity version bound into the shot-reference bundle. A visually similar
reference from another version is never substituted: the expectations built here
carry the version ID, and the pipeline rejects a bundle whose references are not
approved for that version before it gets this far.

When the approved evidence is itself ambiguous - a low-confidence bible, an
unresolved trait - this module says so rather than inventing certainty, and the
pipeline turns that into ``REVIEW``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from services.qa.contracts import AuthoritativeQAInputs, ResolvedReference
from vidgen.contracts.storyboard import StoryboardShot

#: Below this the approved T19 evidence cannot support a confident identity call.
AMBIGUOUS_BIBLE_CONFIDENCE = 0.70


@dataclass(frozen=True, slots=True)
class CharacterIdentityExpectation:
    """Everything identity QA must check for one required character."""

    character_id: UUID
    identity_version_id: UUID
    display_name: str
    stable_traits: dict[str, str]
    required_wardrobe: tuple[str, ...] = ()
    required_injuries: tuple[str, ...] = ()
    required_accessories: tuple[str, ...] = ()
    required_props: tuple[str, ...] = ()
    hairstyle: str | None = None
    disguise: str | None = None
    emotional_state: str | None = None
    reference_asset_ids: tuple[UUID, ...] = ()
    evidence_confidence: float = 1.0
    ambiguities: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ambiguous(self) -> bool:
        return self.evidence_confidence < AMBIGUOUS_BIBLE_CONFIDENCE or bool(self.ambiguities)

    def summary(self) -> str:
        """A bounded, structured description sent to the visual agent."""
        parts = [f"{self.display_name}"]
        for key in ("face", "hair", "skin_tone", "body", "age"):
            value = self.stable_traits.get(key)
            if value:
                parts.append(f"{key}={value}")
        if self.hairstyle:
            parts.append(f"hairstyle={self.hairstyle}")
        if self.required_wardrobe:
            parts.append("wardrobe=" + "|".join(self.required_wardrobe))
        if self.required_accessories:
            parts.append("accessories=" + "|".join(self.required_accessories))
        if self.required_injuries:
            parts.append("injuries=" + "|".join(self.required_injuries))
        if self.required_props:
            parts.append("props=" + "|".join(self.required_props))
        if self.disguise:
            parts.append(f"disguise={self.disguise}")
        if self.emotional_state:
            parts.append(f"emotion={self.emotional_state}")
        return "; ".join(parts)[:1024]


def _traits(bible: dict[str, Any]) -> dict[str, str]:
    traits = bible.get("stable_traits", {})
    if not isinstance(traits, dict):
        return {}
    resolved: dict[str, str] = {}
    for key, value in traits.items():
        if value is None:
            continue
        resolved[str(key)] = (
            ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        )
    return resolved


def _references_for(references: tuple[ResolvedReference, ...], entity_id: UUID) -> tuple[UUID, ...]:
    return tuple(
        item.asset_id
        for item in references
        if item.entity_id == entity_id and item.role in {"character_identity", "character_state"}
    )


def build_character_expectations(
    inputs: AuthoritativeQAInputs, identity_rows: dict[UUID, dict[str, Any]]
) -> tuple[CharacterIdentityExpectation, ...]:
    """Build one expectation per required character from approved T19 records."""
    expectations: list[CharacterIdentityExpectation] = []
    for snapshot in inputs.character_state_snapshots:
        version_id = snapshot["identity_version_id"]
        row = identity_rows.get(version_id)
        if row is None:
            continue
        bible = row.get("identity", {})
        state = snapshot.get("state", {})
        character_id = UUID(str(bible.get("character_id") or snapshot["identity_version_id"]))
        ambiguities = tuple(
            str(item.get("field", "unknown")) for item in bible.get("ambiguities", [])
        )
        expectations.append(
            CharacterIdentityExpectation(
                character_id=character_id,
                identity_version_id=version_id,
                display_name=str(bible.get("display_name", "character")),
                stable_traits=_traits(bible),
                required_wardrobe=tuple(str(item) for item in state.get("wardrobe", [])),
                required_injuries=tuple(str(item) for item in state.get("injuries", [])),
                required_accessories=tuple(str(item) for item in state.get("dirt_or_damage", [])),
                required_props=tuple(str(item) for item in state.get("carried_props", [])),
                hairstyle=state.get("hairstyle") or None,
                disguise=state.get("disguise") or None,
                emotional_state=state.get("emotional_state") or None,
                reference_asset_ids=_references_for(inputs.references, character_id),
                evidence_confidence=float(bible.get("confidence", 1.0)),
                ambiguities=ambiguities,
            )
        )
    return tuple(expectations)


def required_character_count(shot: StoryboardShot) -> int:
    """The exact number of characters the T13 shot requires on screen."""
    return len(shot.incoming_continuity.present_character_ids)


def missing_identity_evidence(
    shot: StoryboardShot, expectations: tuple[CharacterIdentityExpectation, ...]
) -> tuple[UUID, ...]:
    """Required characters with no approved T19 reference asset to compare against."""
    covered = {item.character_id for item in expectations if item.reference_asset_ids}
    return tuple(
        character_id
        for character_id in shot.incoming_continuity.present_character_ids
        if character_id not in covered
    )


def ambiguous_expectations(
    expectations: tuple[CharacterIdentityExpectation, ...],
) -> tuple[str, ...]:
    """Human-readable reasons the approved identity evidence cannot settle a call."""
    reasons: list[str] = []
    for expectation in expectations:
        if expectation.evidence_confidence < AMBIGUOUS_BIBLE_CONFIDENCE:
            reasons.append(
                f"approved identity evidence for {expectation.display_name} has confidence "
                f"{expectation.evidence_confidence:.2f}"
            )
        for field_name in expectation.ambiguities:
            reasons.append(
                f"approved identity for {expectation.display_name} leaves {field_name} unresolved"
            )
    return tuple(reasons[:8])
