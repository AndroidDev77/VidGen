import re
from uuid import uuid4

import pytest

from services.animation.motion_prompt import (
    CONTINUITY_STATEMENT,
    STYLE_TAG_MAX_CHARACTERS,
    STYLE_TAG_MAX_WORDS,
    compile_motion_prompt,
    motion_style_tag,
)
from services.animation.providers import CAPABILITIES, capability_for, prompt_length
from vidgen.contracts.animation import MotionIntent, RunwayModel

#: The ~330 character project style paragraph that overflowed real shots.
PROJECT_STYLE = (
    "Bold flat 2D cartoon animation with thick black outlines, saturated primary "
    "colours, exaggerated rubbery expressions, simple geometric backgrounds, "
    "high-contrast cel shading, playful squash-and-stretch posing, a warm "
    "late-afternoon palette, and a hand-drawn texture that evokes classic "
    "Saturday-morning television while staying crisp at vertical phone resolution."
)
NEGATION = re.compile(r"\b(no|not|never|avoid|without|none|nothing|don't|doesn't|won't)\b", re.I)


def long_intent(**overrides: object) -> MotionIntent:
    """Shot material that exceeded the provider limit under the old template."""
    values: dict[str, object] = {
        "shot_id": uuid4(),
        "shot_sequence": 7,
        "visual_purpose": "reaction",
        "primary_action": "the host slams both palms on the desk and leans toward the lens",
        "start_pose": "seated upright, hands folded on the desk",
        "expected_end_pose": "half standing, palms flat, face close to camera",
        "camera_movement": "slow push in toward the host's face",
        "motion_intensity": "high",
        "style_lock": PROJECT_STYLE,
        "environment_motion": [
            "papers flutter off the desk edge",
            "the desk lamp sways on its arm",
            "coffee sloshes in the mug",
            "background monitor flickers between two graphs",
        ],
        "timing_beats": [
            "0-1s the host inhales",
            "1-2s palms hit the desk",
            "2-4s the host rises and leans in",
            "4-5s the host holds a wide-eyed stare",
        ],
        "continuity_invariants": [
            "preserve character identity, face, skin tone, hair, clothing, body "
            "proportions, props, palette, and environment geometry",
            f"end characters: {uuid4()},{uuid4()}; location: {uuid4()}; emotion: outraged",
        ],
        "negative_motion_constraints": [
            "no camera shake",
            "the host must not leave the chair entirely",
            "avoid lip movement",
        ],
    }
    values.update(overrides)
    return MotionIntent.model_validate(values)


def old_template_length(intent: MotionIntent) -> int:
    """The length the pre-v3 template produced, for the regression fixture."""
    sections = [
        f"Action: {intent.primary_action}.",
        f"Movement: {intent.start_pose} to {intent.expected_end_pose}.",
        f"Camera: {intent.camera_movement}.",
        "Timing: " + "; ".join(intent.timing_beats) + ".",
        "Environment: " + "; ".join(intent.environment_motion) + ".",
        f"Style: {intent.style_lock}.",
        "Preserve: " + "; ".join(intent.continuity_invariants) + ".",
        "Never: "
        + "; ".join(
            [
                *intent.negative_motion_constraints,
                *("cuts", "scene changes", "new subjects", "new objects", "text"),
                *("morphing", "wardrobe changes", "style changes"),
            ]
        )
        + ".",
    ]
    return len(" ".join(sections))


def test_runway_models_share_the_verified_prompt_limit() -> None:
    assert {key: item.prompt_characters for key, item in CAPABILITIES.items()} == {
        "gen4_turbo": 1000,
        "gen4.5": 1000,
    }


def test_prompt_length_counts_utf16_code_units() -> None:
    assert prompt_length("abc") == 3
    assert prompt_length("café") == 4
    assert prompt_length("\U0001f600") == 2


def test_style_tag_is_a_short_deterministic_clause() -> None:
    tag = motion_style_tag(PROJECT_STYLE)
    assert tag == "Bold flat 2D cartoon animation"
    assert tag == motion_style_tag(PROJECT_STYLE)
    assert len(tag.split()) <= STYLE_TAG_MAX_WORDS
    assert len(tag) <= STYLE_TAG_MAX_CHARACTERS
    assert motion_style_tag("") == ""
    assert motion_style_tag("  flat   cel animation ") == "flat cel animation"
    assert motion_style_tag("Watercolour; soft edges") == "Watercolour"
    assert motion_style_tag("x" * 200) == ""


@pytest.mark.parametrize("model", list(RunwayModel))
def test_overflowing_shot_still_compiles_with_required_motion_and_continuity(
    model: RunwayModel,
) -> None:
    limit = capability_for(model).prompt_characters
    item = long_intent()
    assert old_template_length(item) > limit
    package = compile_motion_prompt(item, limit=limit)
    assert prompt_length(package.prompt) <= limit
    assert f"Action: {item.primary_action}." in package.prompt
    assert f"Movement: {item.start_pose} to {item.expected_end_pose}." in package.prompt
    assert "Preserve: " + "; ".join(item.continuity_invariants) + "." in package.prompt
    assert package.prompt.endswith(CONTINUITY_STATEMENT)
    # The paragraph is gone; at most the trimmed-first style tag remains.
    assert PROJECT_STYLE not in package.prompt


def test_trimming_follows_importance_order_and_is_recorded() -> None:
    item = long_intent(style_lock=motion_style_tag(PROJECT_STYLE))
    full = compile_motion_prompt(item, limit=10_000)
    assert full.diagnostics == ["negative_motion_constraints_not_rendered:3"]
    assert "Style: Bold flat 2D cartoon animation." in full.prompt
    # One unit short: only the style tag goes.
    tight = compile_motion_prompt(item, limit=prompt_length(full.prompt) - 1)
    assert "Style:" not in tight.prompt
    assert "Environment: " + "; ".join(item.environment_motion) + "." in tight.prompt
    assert tight.diagnostics[0] == "trimmed_style:1"
    # Much tighter: environment items go last-first before any timing beat.
    required = compile_motion_prompt(
        item.model_copy(update={"style_lock": "", "environment_motion": [], "timing_beats": []}),
        limit=10_000,
    )
    with_one_beat = compile_motion_prompt(
        item.model_copy(
            update={
                "style_lock": "",
                "environment_motion": [],
                "timing_beats": item.timing_beats[:1],
            }
        ),
        limit=10_000,
    )
    package = compile_motion_prompt(item, limit=prompt_length(with_one_beat.prompt))
    assert package.prompt == with_one_beat.prompt
    assert package.diagnostics[:3] == [
        "trimmed_style:1",
        "trimmed_environment:4",
        "trimmed_timing:3",
    ]
    assert "Camera: " in package.prompt
    # Only the required material: camera is the last optional section to go.
    bare = compile_motion_prompt(
        item, limit=prompt_length(required.prompt) - len(f" Camera: {item.camera_movement}.")
    )
    assert "Camera:" not in bare.prompt
    assert "trimmed_camera:1" in bare.diagnostics


def test_only_required_material_exceeding_the_limit_fails() -> None:
    item = long_intent(primary_action="the host gestures " * 80)
    with pytest.raises(ValueError, match="invalid_motion_prompt"):
        compile_motion_prompt(item, limit=1000)


def test_compiled_prompt_is_deterministic() -> None:
    item = long_intent()
    assert compile_motion_prompt(item, limit=1000) == compile_motion_prompt(item, limit=1000)


@pytest.mark.parametrize("limit", [1000, 10_000])
def test_compiled_prompt_contains_no_negative_phrasing(limit: int) -> None:
    item = long_intent(style_lock=motion_style_tag(PROJECT_STYLE))
    package = compile_motion_prompt(item, limit=limit)
    assert NEGATION.search(package.prompt) is None, package.prompt
    assert "Never" not in package.prompt
    for constraint in item.negative_motion_constraints:
        assert constraint not in package.prompt
    # The shot's constraints are kept on the hashed, persisted intent.
    assert package.intent.negative_motion_constraints == item.negative_motion_constraints
