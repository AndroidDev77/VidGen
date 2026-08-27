"""Deterministic motion-only prompt compiler."""

from __future__ import annotations

import hashlib

from vidgen.contracts.animation import MotionIntent, MotionPromptPackage

COMPILER_VERSION = "1.0.0"
TEMPLATE_VERSION = "runway-motion-v1"


def compile_motion_prompt(intent: MotionIntent, *, limit: int = 1000) -> MotionPromptPackage:
    sections = [
        f"Action: {intent.primary_action}.",
        f"Movement: {intent.start_pose} to {intent.expected_end_pose}.",
        f"Camera: {intent.camera_movement}.",
        "Timing: " + "; ".join(intent.timing_beats) + "." if intent.timing_beats else "",
        "Environment: " + "; ".join(intent.environment_motion) + "."
        if intent.environment_motion
        else "",
        "Preserve: " + "; ".join(intent.continuity_invariants) + "."
        if intent.continuity_invariants
        else "",
        "Never: "
        + "; ".join(
            [
                *intent.negative_motion_constraints,
                "cuts",
                "scene changes",
                "new subjects",
                "new objects",
                "text",
                "morphing",
                "wardrobe changes",
                "style changes",
            ]
        )
        + ".",
    ]
    prompt = " ".join(x for x in sections if x)
    if len(prompt) > limit:
        raise ValueError(
            "invalid_motion_prompt: required motion and continuity "
            "constraints exceed provider limit"
        )
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    return MotionPromptPackage(
        intent=intent,
        compiler_version=COMPILER_VERSION,
        template_version=TEMPLATE_VERSION,
        prompt=prompt,
        prompt_hash=digest,
    )
