"""Deterministic T13 validation.

Nothing here asks a model whether output is acceptable. Every rule is a
structural or referential check whose diagnostics are precise enough for a
targeted repair of one segment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from services.storyboard.retimer import ShotTiming
from vidgen.contracts.storyboard import (
    ContinuityState,
    StoryboardShot,
    StoryboardShotProposal,
    StoryboardValidationDiagnostic,
    StoryboardValidationReport,
    VisualProviderCapability,
)

VALIDATOR_VERSION = "storyboard-validator/1.0.0"

#: Diagnostics a Storyboard Director can plausibly fix by re-proposing a segment.
REPAIRABLE_CODES = frozenset(
    {
        "narration_coverage_gap",
        "invalid_overlap",
        "impossible_duration_allocation",
        "unsupported_provider_duration",
        "excessive_character_count",
        "too_many_references",
        "missing_continuity_state",
        "invalid_character_reference",
        "invalid_location_reference",
        "missing_evidence_reference",
        "provider_schema_failure",
        "continuity_contradiction",
        "unsupported_camera_movement",
        "unsupported_transition",
        "word_range_gap",
    }
)


@dataclass(frozen=True, slots=True)
class SegmentValidationContext:
    segment_sequence: int
    narration_duration_us: int
    word_count: int
    capability: VisualProviderCapability
    available_character_ids: frozenset[UUID]
    available_location_ids: frozenset[UUID]
    valid_evidence_ids: frozenset[UUID]
    incoming_continuity: ContinuityState
    anonymous_speaker: bool = False
    checked_continuity_fields: tuple[str, ...] = field(
        default=("location_id", "sub_location", "time_of_day", "screen_direction")
    )


def _diagnostic(
    code: str,
    message: str,
    *,
    entity_path: str,
    segment_sequence: int,
    shot_sequence: int = -1,
    severity: str = "error",
    measured_us: int | None = None,
    expected_us: int | None = None,
) -> StoryboardValidationDiagnostic:
    return StoryboardValidationDiagnostic(
        code=code,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        repairable=code in REPAIRABLE_CODES,
        message=message,
        entity_path=entity_path,
        segment_sequence=segment_sequence,
        shot_sequence=shot_sequence,
        measured_us=measured_us,
        expected_us=expected_us,
    )


def validate_proposals(
    proposals: list[StoryboardShotProposal], context: SegmentValidationContext
) -> list[StoryboardValidationDiagnostic]:
    """Check creative proposals against capabilities, references, and continuity."""
    diagnostics: list[StoryboardValidationDiagnostic] = []
    capability = context.capability
    for proposal in proposals:
        index = proposal.proposal_sequence
        path = f"segments[{context.segment_sequence}].proposals[{index}]"
        characters = proposal.character_reference_ids
        if len(characters) > capability.max_characters_per_shot:
            diagnostics.append(
                _diagnostic(
                    "excessive_character_count",
                    f"shot requests {len(characters)} characters but capability profile "
                    f"{capability.capability_profile_id!r} supports at most "
                    f"{capability.max_characters_per_shot}",
                    entity_path=f"{path}.character_reference_ids",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=index,
                )
            )
        reference_images = len(characters) + (1 if proposal.location_reference_id else 0)
        if reference_images > capability.max_reference_images:
            diagnostics.append(
                _diagnostic(
                    "too_many_references",
                    f"shot requests {reference_images} reference images but capability profile "
                    f"{capability.capability_profile_id!r} supports at most "
                    f"{capability.max_reference_images}",
                    entity_path=f"{path}.character_reference_ids",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=index,
                )
            )
        for character_id in characters:
            if character_id not in context.available_character_ids:
                diagnostics.append(
                    _diagnostic(
                        "invalid_character_reference",
                        f"character {character_id} is not part of the selected episode model",
                        entity_path=f"{path}.character_reference_ids",
                        segment_sequence=context.segment_sequence,
                        shot_sequence=index,
                    )
                )
        if context.anonymous_speaker and characters:
            diagnostics.append(
                _diagnostic(
                    "invalid_character_reference",
                    "the narration segment has an anonymous speaker; a shot must not assign it "
                    "a named character identity",
                    entity_path=f"{path}.character_reference_ids",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=index,
                )
            )
        if (
            proposal.location_reference_id is not None
            and proposal.location_reference_id not in context.available_location_ids
        ):
            diagnostics.append(
                _diagnostic(
                    "invalid_location_reference",
                    f"location {proposal.location_reference_id} is not part of the selected "
                    "episode model",
                    entity_path=f"{path}.location_reference_id",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=index,
                )
            )
        for reference in proposal.evidence_references:
            if (
                reference.reference_type in ("scene_evidence", "evidence_package")
                and reference.reference_id not in context.valid_evidence_ids
            ):
                diagnostics.append(
                    _diagnostic(
                        "missing_evidence_reference",
                        f"evidence reference {reference.reference_id} no longer exists in the "
                        "selected T09 evidence package",
                        entity_path=f"{path}.evidence_references",
                        segment_sequence=context.segment_sequence,
                        shot_sequence=index,
                    )
                )
        diagnostics.extend(_camera_diagnostics(proposal, context, path))
        diagnostics.extend(_transition_diagnostics(proposal, context, path))
        diagnostics.extend(_continuity_declaration_diagnostics(proposal, context, path))
    diagnostics.extend(_continuity_chain_diagnostics(proposals, context))
    return diagnostics


def _camera_diagnostics(
    proposal: StoryboardShotProposal, context: SegmentValidationContext, path: str
) -> list[StoryboardValidationDiagnostic]:
    capability = context.capability
    movement = proposal.camera.movement
    if movement == "static":
        return []
    if (
        not capability.supports_camera_motion
        or movement not in capability.supported_camera_movements
    ):
        return [
            _diagnostic(
                "unsupported_camera_movement",
                f"camera movement {movement!r} is not supported by capability profile "
                f"{capability.capability_profile_id!r}",
                entity_path=f"{path}.camera.movement",
                segment_sequence=context.segment_sequence,
                shot_sequence=proposal.proposal_sequence,
            )
        ]
    return []


def _transition_diagnostics(
    proposal: StoryboardShotProposal, context: SegmentValidationContext, path: str
) -> list[StoryboardValidationDiagnostic]:
    capability = context.capability
    diagnostics: list[StoryboardValidationDiagnostic] = []
    for name, plan in (
        ("transition_in", proposal.transition_in),
        ("transition_out", proposal.transition_out),
    ):
        if plan.kind not in capability.supported_transitions:
            diagnostics.append(
                _diagnostic(
                    "unsupported_transition",
                    f"transition {plan.kind!r} is not supported by capability profile "
                    f"{capability.capability_profile_id!r}",
                    entity_path=f"{path}.{name}.kind",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=proposal.proposal_sequence,
                )
            )
    return diagnostics


def _continuity_declaration_diagnostics(
    proposal: StoryboardShotProposal, context: SegmentValidationContext, path: str
) -> list[StoryboardValidationDiagnostic]:
    diagnostics: list[StoryboardValidationDiagnostic] = []
    incoming = proposal.incoming_continuity
    outgoing = proposal.expected_outgoing_continuity
    missing = [
        character_id
        for character_id in proposal.character_reference_ids
        if character_id not in incoming.present_character_ids
    ]
    if missing:
        diagnostics.append(
            _diagnostic(
                "missing_continuity_state",
                "every referenced character must be declared present in the incoming continuity "
                f"state; missing {', '.join(str(item) for item in missing)}",
                entity_path=f"{path}.incoming_continuity.present_character_ids",
                segment_sequence=context.segment_sequence,
                shot_sequence=proposal.proposal_sequence,
            )
        )
    if incoming.location_id is not None and outgoing.location_id is None:
        diagnostics.append(
            _diagnostic(
                "missing_continuity_state",
                "a shot that enters with a location must declare its expected outgoing location",
                entity_path=f"{path}.expected_outgoing_continuity.location_id",
                segment_sequence=context.segment_sequence,
                shot_sequence=proposal.proposal_sequence,
            )
        )
    if (
        proposal.location_reference_id is not None
        and incoming.location_id is not None
        and incoming.location_id != proposal.location_reference_id
    ):
        diagnostics.append(
            _diagnostic(
                "continuity_contradiction",
                "the shot's location reference contradicts its declared incoming location",
                entity_path=f"{path}.location_reference_id",
                segment_sequence=context.segment_sequence,
                shot_sequence=proposal.proposal_sequence,
            )
        )
    return diagnostics


def _continuity_chain_diagnostics(
    proposals: list[StoryboardShotProposal], context: SegmentValidationContext
) -> list[StoryboardValidationDiagnostic]:
    """Consecutive shots must not introduce unexplained contradictions."""
    diagnostics: list[StoryboardValidationDiagnostic] = []
    previous = context.incoming_continuity
    for proposal in proposals:
        diagnostics.extend(
            _compare_continuity(
                previous,
                proposal.incoming_continuity,
                context=context,
                shot_sequence=proposal.proposal_sequence,
                entity_path=(
                    f"segments[{context.segment_sequence}]"
                    f".proposals[{proposal.proposal_sequence}].incoming_continuity"
                ),
            )
        )
        previous = proposal.expected_outgoing_continuity
    return diagnostics


def _compare_continuity(
    previous: ContinuityState,
    current: ContinuityState,
    *,
    context: SegmentValidationContext,
    shot_sequence: int,
    entity_path: str,
) -> list[StoryboardValidationDiagnostic]:
    diagnostics: list[StoryboardValidationDiagnostic] = []
    explained = {note.code for note in current.unresolved_warnings}
    for name in context.checked_continuity_fields:
        before = getattr(previous, name)
        after = getattr(current, name)
        if before in (None, "", "unspecified", "neutral") or before == after:
            continue
        if name in explained:
            continue
        diagnostics.append(
            _diagnostic(
                "continuity_contradiction",
                f"continuity field {name!r} changed from {before!r} to {after!r} between "
                "consecutive shots without an explaining continuity warning",
                entity_path=f"{entity_path}.{name}",
                segment_sequence=context.segment_sequence,
                shot_sequence=shot_sequence,
            )
        )
    previous_states = {state.character_id: state for state in previous.character_appearance_states}
    for state in current.character_appearance_states:
        earlier = previous_states.get(state.character_id)
        if earlier is None or earlier.appearance_state_id == state.appearance_state_id:
            continue
        if "appearance_state" in explained:
            continue
        diagnostics.append(
            _diagnostic(
                "continuity_contradiction",
                f"character {state.character_id} changed appearance state from "
                f"{earlier.appearance_state_id!r} to {state.appearance_state_id!r} without an "
                "explaining continuity warning",
                entity_path=f"{entity_path}.character_appearance_states",
                segment_sequence=context.segment_sequence,
                shot_sequence=shot_sequence,
            )
        )
    return diagnostics


def validate_outgoing_handoff(
    proposals: list[StoryboardShotProposal],
    declared_outgoing: ContinuityState,
    context: SegmentValidationContext,
) -> list[StoryboardValidationDiagnostic]:
    """The state handed to the next segment must be the last shot's own outcome.

    The result-level outgoing state becomes the next segment's incoming state and
    is hashed into its identity. Unlike the shot-to-shot chain, where filling in a
    previously unset field is a legitimate refinement, any difference here is
    drift: the segment would hand its successor a state no shot in it produced.
    So this compares exactly rather than reusing the chain's "unset is fine" rule.
    """
    if not proposals:
        return []
    final = max(proposals, key=lambda item: item.proposal_sequence)
    expected = final.expected_outgoing_continuity
    path = f"segments[{context.segment_sequence}].expected_outgoing_continuity"
    diagnostics: list[StoryboardValidationDiagnostic] = []
    for name in (*context.checked_continuity_fields, "present_character_ids"):
        mine = getattr(expected, name)
        theirs = getattr(declared_outgoing, name)
        if mine == theirs:
            continue
        diagnostics.append(
            _diagnostic(
                "continuity_contradiction",
                f"the segment hands its successor {name}={theirs!r}, but its final shot "
                f"expects {mine!r}",
                entity_path=f"{path}.{name}",
                segment_sequence=context.segment_sequence,
                shot_sequence=final.proposal_sequence,
            )
        )
    mine_states = {
        state.character_id: state.appearance_state_id
        for state in expected.character_appearance_states
    }
    theirs_states = {
        state.character_id: state.appearance_state_id
        for state in declared_outgoing.character_appearance_states
    }
    if mine_states != theirs_states:
        diagnostics.append(
            _diagnostic(
                "continuity_contradiction",
                "the segment hands its successor character appearance states its final shot "
                "does not expect",
                entity_path=f"{path}.character_appearance_states",
                segment_sequence=context.segment_sequence,
                shot_sequence=final.proposal_sequence,
            )
        )
    return diagnostics


def validate_segment_timing(
    shots: list[ShotTiming], context: SegmentValidationContext
) -> list[StoryboardValidationDiagnostic]:
    """Re-check the solved timing independently of the solver that produced it."""
    diagnostics: list[StoryboardValidationDiagnostic] = []
    capability = context.capability
    cursor = 0
    for shot in shots:
        path = f"segments[{context.segment_sequence}].shots[{shot.shot_sequence}]"
        if shot.usable_duration_us <= 0:
            diagnostics.append(
                _diagnostic(
                    "nonpositive_duration",
                    "solved shot has zero or negative usable duration",
                    entity_path=f"{path}.usable_duration_us",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=shot.shot_sequence,
                    measured_us=shot.usable_duration_us,
                )
            )
        if shot.start_us < cursor:
            diagnostics.append(
                _diagnostic(
                    "invalid_overlap",
                    "solved shots overlap",
                    entity_path=f"{path}.start_us",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=shot.shot_sequence,
                    measured_us=shot.start_us,
                    expected_us=cursor,
                )
            )
        elif shot.start_us > cursor:
            diagnostics.append(
                _diagnostic(
                    "narration_coverage_gap",
                    "solved shots leave an unexplained narration gap",
                    entity_path=f"{path}.start_us",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=shot.shot_sequence,
                    measured_us=shot.start_us,
                    expected_us=cursor,
                )
            )
        if not capability.is_supported_duration(shot.requested_generation_duration_us):
            diagnostics.append(
                _diagnostic(
                    "unsupported_provider_duration",
                    f"requested generation duration {shot.requested_generation_duration_us} us "
                    f"is not supported by capability profile "
                    f"{capability.capability_profile_id!r}",
                    entity_path=f"{path}.requested_generation_duration_us",
                    segment_sequence=context.segment_sequence,
                    shot_sequence=shot.shot_sequence,
                    measured_us=shot.requested_generation_duration_us,
                )
            )
        cursor = shot.end_us
    if cursor != context.narration_duration_us:
        diagnostics.append(
            _diagnostic(
                "narration_coverage_gap",
                "solved shots do not cover the measured narration duration exactly",
                entity_path=f"segments[{context.segment_sequence}].shots",
                segment_sequence=context.segment_sequence,
                measured_us=cursor,
                expected_us=context.narration_duration_us,
            )
        )
    return diagnostics


def build_report(
    diagnostics: list[StoryboardValidationDiagnostic],
    *,
    checked_segment_sequences: list[int],
    covered_duration_us: int,
    expected_duration_us: int,
) -> StoryboardValidationReport:
    ordered = sorted(
        diagnostics,
        key=lambda item: (item.segment_sequence, item.shot_sequence, item.code, item.entity_path),
    )
    return StoryboardValidationReport(
        valid=not any(item.severity == "error" for item in ordered),
        diagnostics=ordered,
        checked_segment_sequences=sorted(set(checked_segment_sequences)),
        covered_duration_us=covered_duration_us,
        expected_duration_us=expected_duration_us,
    )


def validate_storyboard(
    shots: list[StoryboardShot], total_duration_us: int
) -> list[StoryboardValidationDiagnostic]:
    """Whole-storyboard coverage, ordering, and uniqueness."""
    diagnostics: list[StoryboardValidationDiagnostic] = []
    cursor = 0
    seen: set[UUID] = set()
    for shot in shots:
        if shot.shot_id in seen:
            diagnostics.append(
                _diagnostic(
                    "invalid_overlap",
                    f"duplicate shot identity {shot.shot_id}",
                    entity_path=f"shots[{shot.global_sequence}].shot_id",
                    segment_sequence=shot.segment_sequence,
                    shot_sequence=shot.global_sequence,
                )
            )
        seen.add(shot.shot_id)
        if shot.global_start_us < cursor:
            diagnostics.append(
                _diagnostic(
                    "invalid_overlap",
                    "canonical shots overlap on the project timeline",
                    entity_path=f"shots[{shot.global_sequence}].global_start_us",
                    segment_sequence=shot.segment_sequence,
                    shot_sequence=shot.global_sequence,
                    measured_us=shot.global_start_us,
                    expected_us=cursor,
                )
            )
        elif shot.global_start_us > cursor:
            diagnostics.append(
                _diagnostic(
                    "narration_coverage_gap",
                    "canonical shots leave an unexplained gap on the project timeline",
                    entity_path=f"shots[{shot.global_sequence}].global_start_us",
                    segment_sequence=shot.segment_sequence,
                    shot_sequence=shot.global_sequence,
                    measured_us=shot.global_start_us,
                    expected_us=cursor,
                )
            )
        cursor = shot.global_end_us
    if cursor != total_duration_us:
        diagnostics.append(
            _diagnostic(
                "narration_coverage_gap",
                "the canonical storyboard does not end at the total measured narration duration",
                entity_path="shots",
                segment_sequence=-1,
                measured_us=cursor,
                expected_us=total_duration_us,
            )
        )
    return diagnostics
