"""Deterministic, versioned visual-intent prompt compiler."""
# ruff: noqa: E501 -- prompt template lines intentionally mirror stable output sections.

from __future__ import annotations

import hashlib
import json

from vidgen.contracts.image_generation import (
    ImagePromptPackage,
    ImageReferenceBinding,
    VisualIntent,
)

COMPILER_VERSION = "image-prompt/1.0"
TEMPLATE_VERSION = "keyframe/1.0"


class PromptTooLong(ValueError):
    pass


def _join(values: list[str], fallback: str = "none") -> str:
    return "; ".join(values) if values else fallback


def compile_prompt(
    intent: VisualIntent,
    references: list[ImageReferenceBinding] | None = None,
    *,
    limit: int = 32_000,
) -> ImagePromptPackage:
    refs = sorted(references or [], key=lambda x: (x.order, x.semantic_role, str(x.asset_id)))
    required = [
        f"OUTPUT PURPOSE: Create one animation-ready {intent.keyframe_role.value} keyframe only. {intent.visual_purpose}",
        f"VISUAL STYLE LOCK: {intent.style_lock}",
        f"CHARACTERS: Exactly {intent.visible_character_count} visible characters. Invariants: {_join(intent.character_descriptions)}. States: {_join(intent.character_states)}. Do not introduce unnamed characters.",
        f"LOCATION: {intent.location_description}. Layout invariants: {_join(intent.location_invariants)}.",
        f"SCENE CONTEXT: Emotional state is {intent.emotional_state}. Subject priority: {_join(intent.subject_priority)}.",
        f"COMPOSITION AND CAMERA: {intent.composition}; shot size {intent.shot_size}; camera angle {intent.camera_angle}.",
        f"POSE AND ACTION STATE: Freeze a single instant. Required pose: {intent.pose}. Primary action state: {intent.primary_action}.",
        f"PROPS AND CONTINUITY: {_join(intent.props_and_ownership)}. Assumptions: {_join(intent.continuity_assumptions)}.",
        "ANIMATION READINESS: readable silhouettes, clear subject separation, stable anatomy, and uncluttered edges.",
        f"NEGATIVE CONSTRAINTS: {_join(intent.negative_constraints)}.",
        "OUTPUT RESTRICTIONS: no readable text, captions, logos, borders, or watermarks; output one still image.",
    ]
    optional = [f"POSITIVE DETAIL: {value}" for value in intent.positive_constraints]
    diagnostics: list[str] = []
    prompt = "\n".join(required + optional)
    while len(prompt) > limit and optional:
        optional.pop()
        diagnostics.append("removed_optional_detail")
        prompt = "\n".join(required + optional)
    if len(prompt) > limit:
        raise PromptTooLong(f"required prompt is {len(prompt)} characters, limit is {limit}")
    canonical_intent = intent.model_dump(mode="json")
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    input_hash = hashlib.sha256(
        json.dumps(
            {"intent": canonical_intent, "references": [r.model_dump(mode="json") for r in refs]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ImagePromptPackage(
        visual_intent=intent,
        prompt=prompt,
        prompt_compiler_version=COMPILER_VERSION,
        template_version=TEMPLATE_VERSION,
        references=refs,
        diagnostics=diagnostics,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
    )
