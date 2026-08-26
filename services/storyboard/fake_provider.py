"""Deterministic, credential-free Storyboard Director.

Identical requests always produce byte-identical proposals, so golden tests,
restart tests, and idempotency tests never depend on a paid provider.
"""

from __future__ import annotations

import hashlib
from math import ceil
from uuid import UUID

from services.storyboard.providers import FAKE_DIRECTOR_MODEL
from services.storyboard.retimer import allocate_residual
from vidgen.contracts.episode_analysis import StructuredNote
from vidgen.contracts.storyboard import (
    ActionPlan,
    BeatIntent,
    CameraAngle,
    CameraFraming,
    CameraMovement,
    CameraPlan,
    CharacterAppearanceState,
    ContinuityState,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    StoryboardShotProposal,
    StoryboardSourceReference,
    SubjectPosition,
    TransitionPlan,
    VisualProviderCapability,
)

TARGET_SHOT_DURATION_US = 4_000_000

_FRAMINGS: tuple[CameraFraming, ...] = ("wide", "medium", "medium_close", "close_up", "insert")
_ANGLES: tuple[CameraAngle, ...] = ("eye_level", "low_angle", "high_angle", "over_the_shoulder")
_MOVEMENTS: tuple[CameraMovement, ...] = ("static", "dolly_in", "pan_right", "tracking")
_INTENTS: tuple[BeatIntent, ...] = ("establish", "react", "reveal", "punchline", "continue")


def _seed(*parts: object) -> int:
    material = ":".join(str(part) for part in parts).encode()
    return int(hashlib.sha256(material).hexdigest()[:8], 16)


class FakeStoryboardDirector:
    """A director that plans from measured timing alone, with no network access."""

    name = "fake"
    model = FAKE_DIRECTOR_MODEL

    def __init__(self, *, target_shot_duration_us: int = TARGET_SHOT_DURATION_US) -> None:
        self.target_shot_duration_us = target_shot_duration_us

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        word_count = len(request.word_timings)
        shot_count = self._shot_count(request, word_count)
        word_shares = allocate_residual(word_count, shot_count)
        duration_shares = allocate_residual(request.measured_duration_us, shot_count)
        capability = request.capability
        characters = self._characters(request)
        location_id = request.incoming_continuity.location_id or (
            request.available_location_ids[0] if request.available_location_ids else None
        )
        continuity = self._continuity(request, characters, location_id)
        proposals: list[StoryboardShotProposal] = []
        cursor = 0
        for index in range(shot_count):
            start = cursor
            cursor += word_shares[index]
            seed = _seed(request.idempotency_key, index)
            movement = self._movement(capability, seed)
            proposals.append(
                StoryboardShotProposal(
                    proposal_sequence=index,
                    visual_objective=(
                        f"Show the action behind narration words {start}-{cursor} of segment "
                        f"{request.segment_sequence} rather than restating them."
                    ),
                    desired_duration_us=duration_shares[index],
                    word_start_index=start,
                    word_end_index=cursor,
                    clause_label=self._clause_label(request, start, cursor),
                    importance=round(0.4 + (seed % 5) / 10, 2),
                    camera=CameraPlan(
                        framing=_FRAMINGS[seed % len(_FRAMINGS)],
                        angle=_ANGLES[(seed // 7) % len(_ANGLES)],
                        movement=movement,
                        movement_intensity="none" if movement == "static" else "subtle",
                    ),
                    action=ActionPlan(
                        subject_action=(
                            f"Beat {index} of narration segment {request.segment_sequence} plays "
                            "out visually."
                        ),
                        beat_intent=_INTENTS[(seed // 3) % len(_INTENTS)],
                    ),
                    transition_in=TransitionPlan(kind="cut"),
                    transition_out=TransitionPlan(kind="cut"),
                    character_reference_ids=list(characters),
                    location_reference_id=location_id,
                    evidence_references=self._evidence(request),
                    incoming_continuity=continuity,
                    expected_outgoing_continuity=continuity,
                    warnings=[],
                )
            )
        return StoryboardProviderResult(
            proposals=proposals,
            expected_incoming_continuity=continuity,
            expected_outgoing_continuity=continuity,
            provider=self.name,
            model=self.model,
            provider_request_id=(
                "fake-storyboard-"
                + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:32]
            ),
            idempotency_key=request.idempotency_key,
            attempt_number=request.attempt_number,
            usage={
                "input_tokens": len(request.narration_text.split()),
                "output_tokens": shot_count * 32,
            },
            redacted_response_metadata={"deterministic": True},
            warnings=[
                StructuredNote(
                    code="deterministic_director",
                    message="proposals were generated without any provider call",
                )
            ],
        )

    def _shot_count(self, request: StoryboardProviderRequest, word_count: int) -> int:
        proposed = max(1, ceil(request.measured_duration_us / self.target_shot_duration_us))
        # Repairs shrink the plan so a capability or reference failure converges.
        for diagnostic in request.validation_diagnostics:
            if diagnostic.code in ("excessive_character_count", "too_many_references"):
                continue
            proposed = max(1, proposed - 1)
        return max(1, min(proposed, word_count))

    def _characters(self, request: StoryboardProviderRequest) -> tuple[UUID, ...]:
        if request.anonymous_speaker_label is not None:
            return ()
        limit = min(
            request.capability.max_characters_per_shot, request.capability.max_reference_images
        )
        if any(
            diagnostic.code in ("excessive_character_count", "too_many_references")
            for diagnostic in request.validation_diagnostics
        ):
            limit = min(limit, 1)
        return tuple(request.available_character_ids[: max(0, limit)])

    def _movement(self, capability: VisualProviderCapability, seed: int) -> CameraMovement:
        supported = capability.supported_camera_movements
        if not capability.supports_camera_motion or not supported:
            return "static"
        allowed = [item for item in _MOVEMENTS if item in supported] or ["static"]
        return allowed[seed % len(allowed)]

    def _clause_label(self, request: StoryboardProviderRequest, start: int, end: int) -> str:
        labelled = {
            boundary.word_index: boundary.label
            for boundary in request.approved_boundaries
            if boundary.label
        }
        for index in range(end - 1, start - 1, -1):
            if index in labelled:
                return labelled[index][:255]
        return ""

    def _continuity(
        self,
        request: StoryboardProviderRequest,
        characters: tuple[UUID, ...],
        location_id: UUID | None,
    ) -> ContinuityState:
        incoming = request.incoming_continuity
        present = list(characters)
        existing = {state.character_id: state for state in incoming.character_appearance_states}
        return ContinuityState(
            present_character_ids=present,
            character_appearance_states=[
                existing.get(
                    character_id,
                    CharacterAppearanceState(
                        character_id=character_id, appearance_state_id="default"
                    ),
                )
                for character_id in present
            ],
            location_id=location_id,
            sub_location=incoming.sub_location,
            time_of_day=incoming.time_of_day,
            props=list(incoming.props),
            subject_positions=[
                SubjectPosition(
                    character_id=character_id,
                    screen_position="center" if index == 0 else "right",
                )
                for index, character_id in enumerate(present)
            ],
            screen_direction=incoming.screen_direction,
            emotional_state=incoming.emotional_state,
            environment_conditions=list(incoming.environment_conditions),
            previous_shot_id=incoming.previous_shot_id,
        )

    @staticmethod
    def _evidence(request: StoryboardProviderRequest) -> list[StoryboardSourceReference]:
        return [
            reference
            for reference in request.evidence_references
            if reference.reference_type in ("scene_evidence", "evidence_package")
        ][:4]
