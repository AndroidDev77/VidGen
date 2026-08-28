"""Minimal, deterministic prompt repair.

A repaired prompt is a *delta*, never a rewrite. The planner starts from the
constraints the original prompt asserted, marks which of them the T20
diagnostics implicate, and changes only those. Every other constraint - the
required identities, the character count, the approved location, the shot
timing, the T19 reference bindings, the safety and provider-capability limits -
is carried through untouched and listed by name in
:class:`~vidgen.contracts.repair.PromptDelta.preserved_constraint_ids`, so a
reviewer can prove nothing else moved.

Two planners live here:

* :class:`DeterministicRepairPlanner` is the default and the test planner. Given
  the same classification it always produces the same delta, which is what makes
  golden repair tests possible.
* :class:`LanguageModelRepairPlanner` is the production option. A configured
  language model may *propose* a delta, but its proposal is parsed into the same
  strict contract and must pass exactly the same deterministic validation before
  a single provider request is made. Free-form conversational rewriting never
  reaches a provider.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, ValidationError

from vidgen.contracts.common import StrictContract
from vidgen.contracts.repair import (
    PromptConstraint,
    PromptConstraintKind,
    PromptDelta,
    RepairClassification,
    RepairDiagnosticCode,
    RepairSeverity,
)
from vidgen.contracts.storyboard import StoryboardShot

DETERMINISTIC_PLANNER_VERSION = "t21-repair-planner-deterministic/1.0"
LANGUAGE_MODEL_PLANNER_VERSION = "t21-repair-planner-model/1.0"

Code = RepairDiagnosticCode

#: Constraint kinds a planner may never remove or rewrite. These are the shot's
#: identity: change one and the repaired shot is a different shot.
IMMUTABLE_KINDS: frozenset[PromptConstraintKind] = frozenset(
    {
        PromptConstraintKind.CHARACTER_IDENTITY,
        PromptConstraintKind.CHARACTER_COUNT,
        PromptConstraintKind.CHARACTER_STATE,
        PromptConstraintKind.LOCATION,
        PromptConstraintKind.TIMING,
        PromptConstraintKind.CONTINUITY,
        PromptConstraintKind.REFERENCE_BINDING,
        PromptConstraintKind.SAFETY,
        PromptConstraintKind.PROVIDER_CAPABILITY,
    }
)

#: Which constraint kinds each diagnostic is allowed to touch. Anything not
#: listed for a diagnostic stays exactly as it was.
TOUCHABLE_KINDS: dict[RepairDiagnosticCode, tuple[PromptConstraintKind, ...]] = {
    Code.WRONG_CHARACTER_IDENTITY: (PromptConstraintKind.NEGATIVE,),
    Code.WRONG_CHARACTER_COUNT: (PromptConstraintKind.NEGATIVE,),
    Code.WRONG_LOCATION: (PromptConstraintKind.NEGATIVE,),
    Code.WRONG_WARDROBE_OR_STATE: (PromptConstraintKind.NEGATIVE,),
    Code.MISSING_OR_INCORRECT_ACTION: (PromptConstraintKind.ACTION,),
    Code.WEAK_MOTION: (PromptConstraintKind.ACTION, PromptConstraintKind.CAMERA),
    Code.COMPOSITION_FAILURE: (PromptConstraintKind.CAMERA,),
    Code.ANATOMY_OR_ARTIFACT_FAILURE: (PromptConstraintKind.NEGATIVE,),
    Code.CONTINUITY_FAILURE: (PromptConstraintKind.NEGATIVE,),
    Code.STYLE_MISMATCH: (PromptConstraintKind.STYLE,),
    Code.PROMPT_OVERCONSTRAINT: (
        PromptConstraintKind.STYLE,
        PromptConstraintKind.NEGATIVE,
        PromptConstraintKind.CAMERA,
    ),
    Code.PROMPT_AMBIGUITY: (PromptConstraintKind.ACTION,),
    Code.PROVIDER_SAFETY_REJECTION: (PromptConstraintKind.ACTION, PromptConstraintKind.STYLE),
    Code.PROVIDER_TIMEOUT_OR_SERVICE_FAILURE: (),
    Code.UNSUPPORTED_PROVIDER_CAPABILITY: (),
    Code.CORRUPT_OR_INCOMPLETE_MEDIA: (),
    Code.IMPOSSIBLE_DURATION_OR_MOTION: (),
    Code.REFERENCE_CONFLICT: (),
}

#: Diagnostics whose most likely cause is the sampling draw, not the wording.
SEED_SENSITIVE: frozenset[RepairDiagnosticCode] = frozenset(
    {Code.WEAK_MOTION, Code.ANATOMY_OR_ARTIFACT_FAILURE, Code.COMPOSITION_FAILURE}
)


class RepairPlanningError(ValueError):
    """A proposed delta failed deterministic validation, so nothing is spent."""


@dataclass(frozen=True, slots=True)
class PromptRepairRequest:
    """Exactly what any planner is allowed to see."""

    classification: RepairClassification
    constraints: tuple[PromptConstraint, ...]
    base_prompt: str
    base_prompt_hash: str
    previous_seed: int | None
    attempt_ordinal: int
    #: A stable per-attempt value used to derive a new seed without randomness.
    attempt_identity: str


class RepairPromptPlanner(Protocol):
    """Produce one minimal, validated prompt delta for one repair attempt."""

    @property
    def version(self) -> str: ...

    def plan(self, request: PromptRepairRequest) -> PromptDelta: ...


# --- constraint extraction ---------------------------------------------------
def extract_constraints(shot: StoryboardShot, *, capability_profile: str) -> list[PromptConstraint]:
    """Derive the canonical constraint list one repaired prompt must respect.

    The list is ordered and its IDs are stable, so two runs over the same shot
    produce the same constraint IDs and therefore the same, comparable delta.
    """
    constraints: list[PromptConstraint] = []
    for index, character_id in enumerate(shot.character_reference_ids):
        constraints.append(
            PromptConstraint(
                constraint_id=f"character-identity-{index}",
                kind=PromptConstraintKind.CHARACTER_IDENTITY,
                clause=f"Feature the approved character {character_id} exactly as referenced.",
                mutable=False,
                entity_id=character_id,
                source="t13.character_reference_ids",
            )
        )
    constraints.append(
        PromptConstraint(
            constraint_id="character-count",
            kind=PromptConstraintKind.CHARACTER_COUNT,
            clause=(
                f"Show exactly {len(shot.incoming_continuity.present_character_ids)} "
                "character(s) and no one else."
            ),
            mutable=False,
            source="t13.incoming_continuity.present_character_ids",
        )
    )
    for index, state in enumerate(shot.incoming_continuity.character_appearance_states):
        constraints.append(
            PromptConstraint(
                constraint_id=f"character-state-{index}",
                kind=PromptConstraintKind.CHARACTER_STATE,
                clause=(
                    f"Keep {state.character_id} in {state.wardrobe_state or 'the approved state'}."
                ),
                mutable=False,
                entity_id=state.character_id,
                source="t13.incoming_continuity.character_appearance_states",
            )
        )
    if shot.location_reference_id is not None:
        constraints.append(
            PromptConstraint(
                constraint_id="location",
                kind=PromptConstraintKind.LOCATION,
                clause=f"Stage the shot in the approved location {shot.location_reference_id}.",
                mutable=False,
                entity_id=shot.location_reference_id,
                source="t13.location_reference_id",
            )
        )
    constraints.append(
        PromptConstraint(
            constraint_id="action",
            kind=PromptConstraintKind.ACTION,
            clause=shot.action.subject_action,
            mutable=True,
            source="t13.action.subject_action",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="camera",
            kind=PromptConstraintKind.CAMERA,
            clause=(
                f"{shot.camera.framing} framing, {shot.camera.angle} angle, "
                f"{shot.camera.movement} camera movement at "
                f"{shot.camera.movement_intensity} intensity."
            ),
            mutable=True,
            source="t13.camera",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="timing",
            kind=PromptConstraintKind.TIMING,
            rendered=False,
            clause=(
                f"The shot lasts exactly {shot.usable_duration_us} microseconds of usable "
                f"footage from a {shot.requested_generation_duration_us} microsecond "
                "generation."
            ),
            mutable=False,
            source="t13.timing",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="continuity",
            kind=PromptConstraintKind.CONTINUITY,
            clause=(
                "Preserve incoming continuity and end on the expected outgoing state: "
                f"screen direction {shot.expected_outgoing_continuity.screen_direction}, "
                f"emotion {shot.expected_outgoing_continuity.emotional_state}."
            ),
            mutable=False,
            source="t13.continuity",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="reference-binding",
            kind=PromptConstraintKind.REFERENCE_BINDING,
            rendered=False,
            clause=(
                "Honour the approved T19 reference bundle exactly; never substitute a "
                "different identity or location version."
            ),
            mutable=False,
            source="t19.shot_reference_bundle",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="safety",
            kind=PromptConstraintKind.SAFETY,
            clause="No real-person likeness, no on-screen text, no unsafe depiction.",
            mutable=False,
            source="policy.safety",
        )
    )
    constraints.append(
        PromptConstraint(
            constraint_id="provider-capability",
            kind=PromptConstraintKind.PROVIDER_CAPABILITY,
            rendered=False,
            clause=f"Respect the {capability_profile} provider capability profile.",
            mutable=False,
            source="t15.capability_profile",
        )
    )
    for index, negative in enumerate(
        [str(value) for value in shot.provenance.get("negative_motion_constraints", [])]
    ):
        constraints.append(
            PromptConstraint(
                constraint_id=f"negative-{index}",
                kind=PromptConstraintKind.NEGATIVE,
                clause=negative,
                mutable=True,
                source="t13.provenance.negative_motion_constraints",
            )
        )
    constraints.append(
        PromptConstraint(
            constraint_id="style",
            kind=PromptConstraintKind.STYLE,
            clause="Keep the established animated recap style and palette.",
            mutable=True,
            source="project.style",
        )
    )
    return constraints


def apply_edits(
    constraints: Sequence[PromptConstraint],
    *,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    rewritten: Sequence[tuple[str, str]] = (),
) -> str:
    """Render the canonical prompt text from constraints and a set of edits.

    Deterministic by construction: constraint order is the constraint list's
    order, rewrites replace in place, removals drop in place, and additions are
    appended in the order the planner produced them.
    """
    rewrites = dict(rewritten)
    dropped = set(removed)
    clauses = [
        rewrites.get(constraint.clause, constraint.clause)
        for constraint in constraints
        if constraint.rendered and constraint.clause not in dropped
    ]
    clauses.extend(added)
    return "\n".join(clause.strip() for clause in clauses if clause.strip())


def render_prompt(constraints: Sequence[PromptConstraint], delta: PromptDelta | None = None) -> str:
    """Render the original prompt, or the repaired prompt one delta produces."""
    if delta is None:
        return apply_edits(constraints)
    return apply_edits(
        constraints,
        added=delta.added_clauses,
        removed=delta.removed_clauses,
        rewritten=delta.rewritten_clauses,
    )


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def derive_seed(attempt_identity: str) -> int:
    """A stable, non-random seed derived from the attempt's material identity."""
    return int(attempt_identity[:8], 16)


# --- deterministic validation ------------------------------------------------
def validate_delta(
    delta: PromptDelta,
    request: PromptRepairRequest,
) -> None:
    """Reject any delta that touches something it was not implicated in.

    This runs for every planner, including the configured language model, and it
    runs *before* a provider request. A planner that drops a required identity,
    contradicts an approved location, or edits a clause the diagnostics never
    mentioned fails here and costs nothing.
    """
    by_clause = {constraint.clause: constraint for constraint in request.constraints}
    immutable = {
        constraint.constraint_id
        for constraint in request.constraints
        if constraint.kind in IMMUTABLE_KINDS or not constraint.mutable
    }
    allowed_kinds = set(TOUCHABLE_KINDS.get(request.classification.primary_code, ()))
    touched = set(delta.touched_constraint_ids)
    preserved = set(delta.preserved_constraint_ids)
    # Touching an immutable constraint is the more precise complaint, so it is
    # reported first; dropping one from the preserved set is the same mistake
    # seen from the other side.
    if touched & immutable:
        raise RepairPlanningError(
            f"a repair may not touch immutable constraints: {sorted(touched & immutable)}"
        )
    if not immutable <= preserved:
        missing = sorted(immutable - preserved)
        raise RepairPlanningError(
            f"the repaired prompt does not preserve required constraints: {missing}"
        )
    known = {constraint.constraint_id for constraint in request.constraints}
    if not touched <= known:
        raise RepairPlanningError(
            f"the delta touches unknown constraints: {sorted(touched - known)}"
        )
    for constraint_id in touched:
        kind = next(
            item.kind for item in request.constraints if item.constraint_id == constraint_id
        )
        if kind not in allowed_kinds:
            raise RepairPlanningError(
                f"{request.classification.primary_code.value} does not implicate "
                f"{constraint_id} ({kind.value})"
            )
    for clause in delta.removed_clauses:
        constraint = by_clause.get(clause)
        if constraint is None:
            raise RepairPlanningError("a delta may only remove a clause the prompt asserted")
        if constraint.constraint_id not in touched:
            raise RepairPlanningError(
                f"removing {constraint.constraint_id} without declaring it touched"
            )
    for before, _after in delta.rewritten_clauses:
        constraint = by_clause.get(before)
        if constraint is None:
            raise RepairPlanningError("a delta may only rewrite a clause the prompt asserted")
        if constraint.constraint_id not in touched:
            raise RepairPlanningError(
                f"rewriting {constraint.constraint_id} without declaring it touched"
            )
    _reject_contradictions(delta, request)
    if delta.before_prompt_hash != request.base_prompt_hash:
        raise RepairPlanningError("the delta was planned against a different original prompt")
    expected = prompt_hash(render_prompt(request.constraints, delta))
    if delta.after_prompt_hash != expected:
        raise RepairPlanningError("after_prompt_hash does not match the rendered repaired prompt")
    if delta.seed_changed and delta.new_seed == request.previous_seed:
        raise RepairPlanningError("a seed change must actually change the seed")


#: Words that would negate a preserved constraint if a planner smuggled them in.
_NEGATIONS: tuple[str, ...] = ("do not show", "never show", "omit", "without the", "remove the")


def _reject_contradictions(delta: PromptDelta, request: PromptRepairRequest) -> None:
    """Refuse an added clause that negates a constraint we promised to preserve."""
    preserved = [
        constraint
        for constraint in request.constraints
        if constraint.constraint_id in set(delta.preserved_constraint_ids)
        and constraint.entity_id is not None
    ]
    added = [clause.lower() for clause in delta.added_clauses]
    added.extend(after.lower() for _before, after in delta.rewritten_clauses)
    for constraint in preserved:
        needle = str(constraint.entity_id).lower()
        for clause in added:
            if needle in clause and any(negation in clause for negation in _NEGATIONS):
                raise RepairPlanningError(
                    f"a repaired clause contradicts preserved constraint {constraint.constraint_id}"
                )


# --- the deterministic planner ----------------------------------------------
class DeterministicRepairPlanner:
    """The default, fully deterministic planner. No model, no network, no randomness."""

    @property
    def version(self) -> str:
        return DETERMINISTIC_PLANNER_VERSION

    def plan(self, request: PromptRepairRequest) -> PromptDelta:
        classification = request.classification
        code = classification.primary_code
        allowed = set(TOUCHABLE_KINDS.get(code, ()))
        added: list[str] = []
        removed: list[str] = []
        rewritten: list[tuple[str, str]] = []
        touched: list[str] = []
        structural = classification.severity is RepairSeverity.STRUCTURAL
        for constraint in request.constraints:
            if constraint.kind not in allowed or not constraint.mutable:
                continue
            edit = self._edit(code, constraint.kind, constraint.clause, structural=structural)
            if edit is None:
                continue
            touched.append(constraint.constraint_id)
            if edit == "":
                removed.append(constraint.clause)
            else:
                rewritten.append((constraint.clause, edit))
        added.extend(self._additions(classification))
        seed_changed = structural or code in SEED_SENSITIVE
        new_seed = derive_seed(request.attempt_identity) if seed_changed else None
        if not (added or removed or rewritten) and not seed_changed:
            # A repair that changes nothing would burn an attempt for free.
            raise RepairPlanningError(
                f"no bounded prompt repair exists for {code.value}; route it instead"
            )
        repaired = apply_edits(
            request.constraints, added=added, removed=removed, rewritten=rewritten
        )
        return PromptDelta(
            planner_version=self.version,
            repair_reason=classification.rationale or f"repair {code.value}",
            repair_codes=[
                repair_code
                for diagnostic in classification.diagnostics
                for repair_code in diagnostic.repair_codes
            ][:16],
            source_finding_ids=[
                finding_id
                for diagnostic in classification.diagnostics
                for finding_id in diagnostic.source_finding_ids
            ][:32],
            added_clauses=added,
            removed_clauses=removed,
            rewritten_clauses=rewritten,
            preserved_constraint_ids=[
                constraint.constraint_id
                for constraint in request.constraints
                if constraint.constraint_id not in set(touched)
            ],
            touched_constraint_ids=touched,
            before_prompt_hash=request.base_prompt_hash,
            after_prompt_hash=prompt_hash(repaired),
            seed_changed=seed_changed,
            previous_seed=request.previous_seed,
            new_seed=new_seed,
        )

    @staticmethod
    def _edit(
        code: RepairDiagnosticCode,
        kind: PromptConstraintKind,
        clause: str,
        *,
        structural: bool,
    ) -> str | None:
        """Return the rewritten clause, ``""`` to remove it, or ``None`` to keep it."""
        if code is Code.PROMPT_OVERCONSTRAINT:
            # Simplification is the repair: drop decoration, keep meaning.
            return (
                "" if kind in {PromptConstraintKind.STYLE, PromptConstraintKind.NEGATIVE} else None
            )
        if code is Code.MISSING_OR_INCORRECT_ACTION and kind is PromptConstraintKind.ACTION:
            return f"{clause} The action must be unmistakably visible and completed on screen."
        if code is Code.PROMPT_AMBIGUITY and kind is PromptConstraintKind.ACTION:
            return f"{clause} State one single unambiguous beat; do not imply alternatives."
        if code is Code.WEAK_MOTION and kind is PromptConstraintKind.ACTION:
            return f"{clause} Sustain continuous visible movement for the whole shot."
        if code is Code.WEAK_MOTION and kind is PromptConstraintKind.CAMERA:
            return f"{clause} Add a slow, steady camera move to guarantee visible motion."
        if code is Code.COMPOSITION_FAILURE and kind is PromptConstraintKind.CAMERA:
            simplified = "Simplify to a single clear subject centred in frame."
            return f"{clause} {simplified}" if not structural else simplified
        if code is Code.STYLE_MISMATCH and kind is PromptConstraintKind.STYLE:
            return f"{clause} Match the established palette and line weight exactly."
        if code is Code.PROVIDER_SAFETY_REJECTION and kind in {
            PromptConstraintKind.ACTION,
            PromptConstraintKind.STYLE,
        }:
            return f"{clause} Keep the depiction non-graphic and policy-safe."
        return None

    @staticmethod
    def _additions(classification: RepairClassification) -> list[str]:
        code = classification.primary_code
        additions: dict[RepairDiagnosticCode, list[str]] = {
            Code.WRONG_CHARACTER_IDENTITY: [
                "Match the referenced character's face, hair and skin tone exactly.",
                "Do not substitute, age, or restyle the referenced character.",
            ],
            Code.WRONG_CHARACTER_COUNT: [
                "Do not add background people, crowds, or reflections of other characters.",
            ],
            Code.WRONG_LOCATION: [
                "Match the referenced location's layout and landmarks exactly.",
                "Do not relocate the action to a similar-looking place.",
            ],
            Code.WRONG_WARDROBE_OR_STATE: [
                "Keep the referenced wardrobe, hairstyle and carried props unchanged.",
            ],
            Code.ANATOMY_OR_ARTIFACT_FAILURE: [
                "No distorted faces, extra limbs, merged fingers, or rendered text.",
            ],
            Code.CONTINUITY_FAILURE: [
                "Preserve screen direction and the incoming emotional state without a jump.",
            ],
        }
        return additions.get(code, [])


# --- the configured language-model planner ----------------------------------
class RepairPlannerProposal(StrictContract):
    """The strict structured contract a configured model must answer with.

    A model never returns a prompt. It returns a bounded proposal, which is
    converted into a :class:`PromptDelta` and validated deterministically. There
    is no path from model prose to a provider request.
    """

    schema_version: Literal["1.0"] = "1.0"
    repair_reason: str = Field(min_length=1, max_length=500)
    added_clauses: list[str] = Field(default_factory=list, max_length=8)
    removed_clause_ids: list[str] = Field(default_factory=list, max_length=8)
    rewritten_clauses: list[tuple[str, str]] = Field(default_factory=list, max_length=8)
    change_seed: bool = False


class LanguageModelRepairPlanner:
    """A configured model proposes; deterministic validation decides.

    ``complete`` receives a compact, bounded description of the failure and the
    mutable constraints, and must return JSON conforming to
    :class:`RepairPlannerProposal`. Anything else - prose, an extra field, a
    touched immutable constraint - is rejected before any spend, and the
    deterministic planner is not silently substituted: the caller decides.
    """

    def __init__(
        self,
        complete: Callable[[str], str],
        *,
        model: str,
        version: str = LANGUAGE_MODEL_PLANNER_VERSION,
    ) -> None:
        self._complete = complete
        self._model = model
        self._version = version

    @property
    def version(self) -> str:
        return f"{self._version}:{self._model}"

    def plan(self, request: PromptRepairRequest) -> PromptDelta:
        try:
            proposal = RepairPlannerProposal.model_validate_json(
                self._complete(self.render_instruction(request))
            )
        except ValidationError as error:
            raise RepairPlanningError(
                "the configured repair planner did not answer the structured contract"
            ) from error
        by_id = {item.constraint_id: item for item in request.constraints}
        touched: list[str] = []
        removed: list[str] = []
        for constraint_id in proposal.removed_clause_ids:
            constraint = by_id.get(constraint_id)
            if constraint is None:
                raise RepairPlanningError(f"the planner named an unknown clause {constraint_id!r}")
            touched.append(constraint_id)
            removed.append(constraint.clause)
        rewritten: list[tuple[str, str]] = []
        for constraint_id, after in proposal.rewritten_clauses:
            constraint = by_id.get(constraint_id)
            if constraint is None:
                raise RepairPlanningError(f"the planner named an unknown clause {constraint_id!r}")
            touched.append(constraint_id)
            rewritten.append((constraint.clause, after))
        new_seed = derive_seed(request.attempt_identity) if proposal.change_seed else None
        repaired = apply_edits(
            request.constraints,
            added=proposal.added_clauses,
            removed=removed,
            rewritten=rewritten,
        )
        delta = PromptDelta(
            planner_version=self.version,
            repair_reason=proposal.repair_reason,
            repair_codes=[
                code
                for diagnostic in request.classification.diagnostics
                for code in diagnostic.repair_codes
            ][:16],
            source_finding_ids=[
                finding_id
                for diagnostic in request.classification.diagnostics
                for finding_id in diagnostic.source_finding_ids
            ][:32],
            added_clauses=proposal.added_clauses,
            removed_clauses=removed,
            rewritten_clauses=rewritten,
            preserved_constraint_ids=[
                item.constraint_id
                for item in request.constraints
                if item.constraint_id not in touched
            ],
            touched_constraint_ids=touched,
            before_prompt_hash=request.base_prompt_hash,
            after_prompt_hash=prompt_hash(repaired),
            seed_changed=proposal.change_seed,
            previous_seed=request.previous_seed,
            new_seed=new_seed,
        )
        validate_delta(delta, request)
        return delta

    @staticmethod
    def render_instruction(request: PromptRepairRequest) -> str:
        """The bounded, structured brief the model is allowed to see.

        It carries diagnostics and mutable clauses only: no credentials, no
        asset bytes, no signed URLs, and no immutable constraint the model
        could be tempted to edit.
        """
        payload = {
            "contract": "t21-repair-planner-proposal/1.0",
            "category": request.classification.category.value,
            "severity": request.classification.severity.value,
            "primary_code": request.classification.primary_code.value,
            "diagnostics": [
                {
                    "code": item.code.value,
                    "severity": item.severity,
                    "summary": item.summary,
                }
                for item in request.classification.diagnostics
            ],
            "editable_clauses": [
                {
                    "constraint_id": item.constraint_id,
                    "kind": item.kind.value,
                    "clause": item.clause,
                }
                for item in request.constraints
                if item.mutable
                and item.kind in set(TOUCHABLE_KINDS.get(request.classification.primary_code, ()))
            ],
            "rules": [
                "change only the clauses listed in editable_clauses",
                "never restate or edit an identity, count, location, timing, continuity, "
                "reference, safety or capability constraint",
                "answer with RepairPlannerProposal JSON and nothing else",
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DETERMINISTIC_PLANNER_VERSION",
    "IMMUTABLE_KINDS",
    "LANGUAGE_MODEL_PLANNER_VERSION",
    "SEED_SENSITIVE",
    "TOUCHABLE_KINDS",
    "DeterministicRepairPlanner",
    "LanguageModelRepairPlanner",
    "PromptRepairRequest",
    "RepairPlannerProposal",
    "RepairPlanningError",
    "RepairPromptPlanner",
    "apply_edits",
    "derive_seed",
    "extract_constraints",
    "prompt_hash",
    "render_prompt",
    "validate_delta",
]
