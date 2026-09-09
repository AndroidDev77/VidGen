"""Deterministic, credential-free Storyboard Director.

Identical requests always produce byte-identical proposals, so golden tests,
restart tests, and idempotency tests never depend on a paid provider. The fake
plans the way the production Director is instructed to: it cuts only at the
approved sentence, clause and comedy-beat boundaries it is given, prefers the
pacing preset's target range, lets a punchline or reaction shot run shorter, and
lets an establishing shot run longer. It never divides narration mechanically
into a fixed number of shots when a boundary is available.
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
    BoundaryKind,
    CameraAngle,
    CameraFraming,
    CameraMovement,
    CameraPlan,
    CharacterAppearanceState,
    ContinuityState,
    ShotPacingGuidance,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    StoryboardShotProposal,
    StoryboardSourceReference,
    SubjectPosition,
    TransitionPlan,
    VisualProviderCapability,
)

TARGET_SHOT_DURATION_US = 4_000_000
#: The pacing an old request without guidance is planned at: the normal preset.
_DEFAULT_GUIDANCE = ShotPacingGuidance(
    preset="normal",
    profile_version="shot-pacing/1",
    target_min_duration_us=4_000_000,
    target_max_duration_us=7_000_000,
    hard_max_duration_us=7_500_000,
    min_punchline_duration_us=1_500_000,
    hero_max_duration_us=10_000_000,
)
#: Importance at or above this marks a hero shot for routing and QA.
HERO_IMPORTANCE = 0.85

_FRAMINGS: tuple[CameraFraming, ...] = ("wide", "medium", "medium_close", "close_up", "insert")
_ANGLES: tuple[CameraAngle, ...] = ("eye_level", "low_angle", "high_angle", "over_the_shoulder")
_MOVEMENTS: tuple[CameraMovement, ...] = ("static", "dolly_in", "pan_right", "tracking")
_INTENTS: tuple[BeatIntent, ...] = ("establish", "react", "reveal", "punchline", "continue")
_RANK: dict[BoundaryKind, int] = {"sentence": 3, "clause": 2, "beat": 1, "word": 0}
#: Repair diagnostics that the character and evidence selection already answer;
#: any other diagnostic shrinks the plan so a capability failure converges.
_REFERENCE_DIAGNOSTICS = frozenset({"excessive_character_count", "too_many_references"})


def _seed(*parts: object) -> int:
    material = ":".join(str(part) for part in parts).encode()
    return int(hashlib.sha256(material).hexdigest()[:8], 16)


class FakeStoryboardDirector:
    """A director that plans from measured timing and approved boundaries alone."""

    name = "fake"
    model = FAKE_DIRECTOR_MODEL

    def __init__(self, *, target_shot_duration_us: int = TARGET_SHOT_DURATION_US) -> None:
        self.target_shot_duration_us = target_shot_duration_us

    async def propose(self, request: StoryboardProviderRequest) -> StoryboardProviderResult:
        word_count = len(request.word_timings)
        capability = request.capability
        characters = self._characters(request)
        location_id = request.incoming_continuity.location_id or (
            request.available_location_ids[0] if request.available_location_ids else None
        )
        continuity = self._continuity(request, characters, location_id)
        spans = self._plan_spans(request, word_count)
        proposals: list[StoryboardShotProposal] = []
        for index, (start, cursor, duration_us, kind) in enumerate(spans):
            seed = _seed(request.idempotency_key, index)
            movement = self._movement(capability, seed)
            intent = self._intent(request, index, len(spans), duration_us, kind, seed)
            importance = self._importance(request, index, intent, seed)
            proposals.append(
                StoryboardShotProposal(
                    proposal_sequence=index,
                    visual_objective=(
                        f"Show the action behind narration words {start}-{cursor} of segment "
                        f"{request.segment_sequence} rather than restating them."
                    ),
                    desired_duration_us=duration_us,
                    word_start_index=start,
                    word_end_index=cursor,
                    clause_label=self._clause_label(request, start, cursor),
                    importance=importance,
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
                        beat_intent=intent,
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
                "output_tokens": len(proposals) * 32,
            },
            redacted_response_metadata={
                "deterministic": True,
                "pacing_preset": (request.pacing or _DEFAULT_GUIDANCE).preset,
            },
            warnings=[
                StructuredNote(
                    code="deterministic_director",
                    message="proposals were generated without any provider call",
                )
            ],
        )

    # -- semantic shot planning ---------------------------------------------------

    def _plan_spans(
        self, request: StoryboardProviderRequest, word_count: int
    ) -> list[tuple[int, int, int, BoundaryKind | None]]:
        """Word spans as ``(start, end, duration_us, boundary_kind)``.

        A repair attempt with a non-reference diagnostic falls back to the
        even division the original fake used, shrinking the plan by one shot
        per diagnostic so a capability failure converges deterministically.
        """
        shrinking = [
            item
            for item in request.validation_diagnostics
            if item.code not in _REFERENCE_DIAGNOSTICS
        ]
        if shrinking:
            return self._even_spans(request, word_count, len(shrinking))
        guidance = request.pacing or _DEFAULT_GUIDANCE
        word_ends = self._word_ends(request)
        approved = self._approved(request, word_count)
        if not approved:
            return self._even_spans(request, word_count, 0)
        spans: list[tuple[int, int, int, BoundaryKind | None]] = []
        start_word = 0
        start_us = 0
        total = request.measured_duration_us
        while start_word < word_count:
            choice = self._choose_cut(
                approved,
                word_ends,
                start_word=start_word,
                start_us=start_us,
                end_us=total,
                word_count=word_count,
                guidance=guidance,
            )
            if choice is None:
                spans.append((start_word, word_count, total - start_us, None))
                break
            word_index, kind = choice
            end_us = word_ends[word_index]
            spans.append((start_word, word_index + 1, end_us - start_us, kind))
            start_word = word_index + 1
            start_us = end_us
        return self._absorb_short_tail(spans, guidance)

    @staticmethod
    def _word_ends(request: StoryboardProviderRequest) -> list[int]:
        ends: list[int] = []
        previous = 0
        for timing in sorted(request.word_timings, key=lambda item: item.word_index):
            offset = min(max(timing.offset_us, previous), request.measured_duration_us)
            ends.append(offset)
            previous = offset
        return ends

    @staticmethod
    def _approved(request: StoryboardProviderRequest, word_count: int) -> dict[int, BoundaryKind]:
        """Approved boundaries in measured-word positions, strongest kind wins."""
        position = {
            timing.word_index: index
            for index, timing in enumerate(
                sorted(request.word_timings, key=lambda item: item.word_index)
            )
        }
        kinds: dict[int, BoundaryKind] = {}
        for boundary in request.approved_boundaries:
            index = position.get(boundary.word_index)
            if index is None or index >= word_count - 1:
                continue
            if _RANK[boundary.kind] >= _RANK[kinds.get(index, "word")]:
                kinds[index] = boundary.kind
        return kinds

    @staticmethod
    def _choose_cut(
        approved: dict[int, BoundaryKind],
        word_ends: list[int],
        *,
        start_word: int,
        start_us: int,
        end_us: int,
        word_count: int,
        guidance: ShotPacingGuidance,
    ) -> tuple[int, BoundaryKind] | None:
        """The approved boundary that best serves the pacing preference.

        Preference order: an in-range boundary, strongest kind first and then
        nearest the target midpoint; failing that, a comedy beat that lets a
        punchline or reaction shot run short; failing that, the last boundary
        before the hard maximum so the shot never needs a mechanical split.
        ``None`` keeps the rest of the segment as one shot - the remainder is
        either within range already or has no boundary to cut on.
        """
        remaining = end_us - start_us
        if remaining <= guidance.target_max_duration_us:
            return None
        midpoint = (guidance.target_min_duration_us + guidance.target_max_duration_us) // 2
        candidates = [
            (index, kind, word_ends[index] - start_us)
            for index, kind in sorted(approved.items())
            if start_word <= index < word_count - 1 and word_ends[index] > start_us
        ]
        in_range = [
            item
            for item in candidates
            if guidance.target_min_duration_us <= item[2] <= guidance.target_max_duration_us
        ]
        if in_range:
            index, kind, _ = min(
                in_range, key=lambda item: (-_RANK[item[1]], abs(item[2] - midpoint), item[0])
            )
            return index, kind
        beats = [
            item
            for item in candidates
            if item[1] == "beat"
            and guidance.min_punchline_duration_us <= item[2] < guidance.target_min_duration_us
        ]
        if beats:
            index, kind, _ = max(beats, key=lambda item: (item[2], -item[0]))
            return index, kind
        before_hard_max = [item for item in candidates if item[2] <= guidance.hard_max_duration_us]
        if before_hard_max:
            index, kind, _ = max(before_hard_max, key=lambda item: (item[2], -item[0]))
            return index, kind
        return None

    @staticmethod
    def _absorb_short_tail(
        spans: list[tuple[int, int, int, BoundaryKind | None]], guidance: ShotPacingGuidance
    ) -> list[tuple[int, int, int, BoundaryKind | None]]:
        """A trailing fragment too short to stand alone joins the previous shot.

        Only when the merged shot still fits the hard maximum: an over-long shot
        would just be split again by the retimer, undoing the merge.
        """
        if len(spans) < 2:
            return spans
        *head, (_start, end, duration, kind) = spans
        previous_start, _previous_end, previous_duration, _previous_kind = head[-1]
        if (
            duration < guidance.min_punchline_duration_us
            and previous_duration + duration <= guidance.hard_max_duration_us
        ):
            head[-1] = (previous_start, end, previous_duration + duration, kind)
            return head
        return spans

    def _even_spans(
        self, request: StoryboardProviderRequest, word_count: int, shrink_by: int
    ) -> list[tuple[int, int, int, BoundaryKind | None]]:
        target = (request.pacing or _DEFAULT_GUIDANCE).target_max_duration_us
        proposed = max(1, ceil(request.measured_duration_us / target))
        proposed = max(1, min(proposed - shrink_by, word_count))
        word_shares = allocate_residual(word_count, proposed)
        duration_shares = allocate_residual(request.measured_duration_us, proposed)
        spans: list[tuple[int, int, int, BoundaryKind | None]] = []
        cursor = 0
        for index in range(proposed):
            start = cursor
            cursor += word_shares[index]
            spans.append((start, cursor, duration_shares[index], None))
        return spans

    @staticmethod
    def _intent(
        request: StoryboardProviderRequest,
        index: int,
        count: int,
        duration_us: int,
        kind: BoundaryKind | None,
        seed: int,
    ) -> BeatIntent:
        guidance = request.pacing or _DEFAULT_GUIDANCE
        if index == 0 and request.segment_sequence == 0:
            return "establish"
        if kind == "beat":
            return "punchline"
        if duration_us < guidance.target_min_duration_us:
            return "react"
        if index == count - 1 and kind is None and count > 1:
            return "continue"
        return _INTENTS[(seed // 3) % len(_INTENTS)]

    @staticmethod
    def _importance(
        request: StoryboardProviderRequest, index: int, intent: BeatIntent, seed: int
    ) -> float:
        # The opening establishing shot of the recap is the one hero shot the
        # fake designates; everything else stays below the hero floor.
        if index == 0 and request.segment_sequence == 0 and intent == "establish":
            return HERO_IMPORTANCE
        return round(0.4 + (seed % 4) / 10, 2)

    # -- references and continuity -----------------------------------------------

    def _characters(self, request: StoryboardProviderRequest) -> tuple[UUID, ...]:
        if request.anonymous_speaker_label is not None:
            return ()
        limit = min(
            request.capability.max_characters_per_shot, request.capability.max_reference_images
        )
        if any(
            diagnostic.code in _REFERENCE_DIAGNOSTICS
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
