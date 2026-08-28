"""T13/T19 continuity comparison inputs for T20.

Continuity QA compares the sampled frames against the T13 incoming state, the
T13 expected outgoing state, the approved T19 identity and state snapshots and,
when one exists, the previous *passing* shot. Only selected compatible attempts
are used for cross-shot comparison: a superseded clip never becomes the baseline.

Future-shot state never fails an earlier shot unless the storyboard explicitly
asks the shot to anticipate it, which the Director records in shot provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from services.qa.contracts import AuthoritativeQAInputs
from vidgen.contracts.storyboard import ContinuityState, StoryboardShot


@dataclass(frozen=True, slots=True)
class LocationExpectation:
    """The approved T19 location version plus the active state snapshot."""

    location_id: UUID | None
    identity_version_id: UUID | None
    display_name: str
    location_type: str | None
    stable_traits: dict[str, str]
    time_of_day: str | None
    lighting: str | None
    weather: str | None
    damage: tuple[str, ...]
    prop_placement: dict[str, str]
    reference_asset_ids: tuple[UUID, ...]
    evidence_confidence: float = 1.0

    def summary(self) -> str:
        parts = [self.display_name]
        if self.location_type:
            parts.append(f"type={self.location_type}")
        for key in ("layout", "architecture", "landmarks", "interior_exterior"):
            value = self.stable_traits.get(key)
            if value:
                parts.append(f"{key}={value}")
        for label, value in (
            ("time_of_day", self.time_of_day),
            ("lighting", self.lighting),
            ("weather", self.weather),
        ):
            if value:
                parts.append(f"{label}={value}")
        if self.damage:
            parts.append("damage=" + "|".join(self.damage))
        if self.prop_placement:
            parts.append(
                "prop_placement="
                + "|".join(f"{key}:{value}" for key, value in sorted(self.prop_placement.items()))
            )
        return "; ".join(parts)[:1024]


@dataclass(frozen=True, slots=True)
class ContinuityExpectation:
    """Everything continuity QA compares, and the baseline it may compare against."""

    incoming: ContinuityState
    outgoing: ContinuityState
    location: LocationExpectation
    previous_shot_id: UUID | None
    previous_video_asset_id: UUID | None
    previous_passed_qa: bool
    anticipates_next_shot: bool

    @property
    def baseline_available(self) -> bool:
        """A previous shot is a usable baseline only when it passed video QA."""
        return self.previous_video_asset_id is not None and self.previous_passed_qa


def summarize_state(state: ContinuityState) -> str:
    """A bounded, structured continuity summary; never free prose from a prompt."""
    parts = [
        "characters=" + ",".join(str(value) for value in state.present_character_ids),
        f"location={state.location_id}",
        f"sub_location={state.sub_location or 'unspecified'}",
        f"time_of_day={state.time_of_day}",
        f"screen_direction={state.screen_direction}",
        f"emotion={state.emotional_state}",
    ]
    if state.character_appearance_states:
        parts.append(
            "appearance="
            + "|".join(
                f"{item.character_id}:{item.wardrobe_state}"
                for item in state.character_appearance_states
            )
        )
    if state.props:
        parts.append(
            "props="
            + "|".join(
                f"{item.prop_id}@{item.owner_character_id or 'scene'}" for item in state.props
            )
        )
    if state.subject_positions:
        parts.append(
            "positions="
            + "|".join(
                f"{item.character_id}:{item.screen_position}/{item.facing}"
                for item in state.subject_positions
            )
        )
    if state.environment_conditions:
        parts.append("environment=" + ",".join(state.environment_conditions))
    return "; ".join(parts)[:2048]


def build_location_expectation(
    inputs: AuthoritativeQAInputs, location_row: dict[str, Any] | None
) -> LocationExpectation:
    bible = (location_row or {}).get("identity", {})
    snapshot = (inputs.location_state_snapshot or {}).get("state", {})
    location_id = (
        UUID(str(bible["location_id"])) if bible.get("location_id") else None
    ) or inputs.shot.location_reference_id
    traits = bible.get("stable_traits", {})
    resolved = {
        str(key): ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        for key, value in (traits.items() if isinstance(traits, dict) else [])
        if value is not None
    }
    return LocationExpectation(
        location_id=location_id,
        identity_version_id=inputs.location_identity_version_id,
        display_name=str(bible.get("display_name", "location")),
        location_type=bible.get("location_type") or None,
        stable_traits=resolved,
        time_of_day=snapshot.get("time_of_day") or None,
        lighting=snapshot.get("lighting") or None,
        weather=snapshot.get("weather") or None,
        damage=tuple(str(item) for item in snapshot.get("damage", [])),
        prop_placement={
            str(key): str(value) for key, value in (snapshot.get("prop_placement") or {}).items()
        },
        reference_asset_ids=tuple(
            item.asset_id
            for item in inputs.references
            if item.role in {"location_identity", "location_state"}
        ),
        evidence_confidence=float(bible.get("confidence", 1.0)),
    )


def anticipates_next_shot(shot: StoryboardShot) -> bool:
    """Whether T13 explicitly asks this shot to anticipate the following state."""
    return bool(shot.provenance.get("anticipates_next_shot", False))


def build_expectation(
    inputs: AuthoritativeQAInputs,
    location_row: dict[str, Any] | None,
    *,
    previous_passed_qa: bool,
) -> ContinuityExpectation:
    return ContinuityExpectation(
        incoming=inputs.shot.incoming_continuity,
        outgoing=inputs.shot.expected_outgoing_continuity,
        location=build_location_expectation(inputs, location_row),
        previous_shot_id=inputs.previous_shot_record.id
        if inputs.previous_shot_record is not None
        else None,
        previous_video_asset_id=inputs.previous_video.canonical_asset_id
        if inputs.previous_video is not None
        else None,
        previous_passed_qa=previous_passed_qa,
        anticipates_next_shot=anticipates_next_shot(inputs.shot),
    )


def required_props(shot: StoryboardShot) -> tuple[str, ...]:
    """The union of shot-level and action-level required prop references."""
    return tuple(sorted({*shot.prop_references, *shot.action.prop_references}))
