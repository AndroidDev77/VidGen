from __future__ import annotations

from uuid import uuid4

import pytest

from services.image_generation.fake_provider import DeterministicFakeImageProvider
from services.image_generation.prompt_compiler import PromptTooLong, compile_prompt
from services.image_generation.providers import GPT_IMAGE_SNAPSHOT, validate_dimensions
from services.image_generation.validation import validate_base64_image
from vidgen.contracts.image_generation import ImageProviderRequest, KeyframeRole, VisualIntent


def intent(role: KeyframeRole = KeyframeRole.FIRST_FRAME) -> VisualIntent:
    return VisualIntent(
        shot_id=uuid4(),
        shot_sequence=2,
        keyframe_role=role,
        visual_purpose="establish the joke",
        style_lock="flat cel animation",
        visible_character_count=1,
        character_descriptions=["Mara: brown skin, black curls, red coat"],
        character_states=["uninjured"],
        location_description="kitchen",
        location_invariants=["window left of sink"],
        props_and_ownership=["Mara owns blue mug"],
        composition="Mara isolated center",
        shot_size="medium",
        camera_angle="eye level",
        subject_priority=["Mara"],
        pose="hand on mug" if role == KeyframeRole.FIRST_FRAME else "mug raised",
        primary_action="about to sip",
        emotional_state="suspicious",
        continuity_assumptions=["red coat remains"],
        negative_constraints=["no extra people"],
    )


def request(key: str = "stable") -> ImageProviderRequest:
    return ImageProviderRequest(
        application_idempotency_key=key,
        project_id=uuid4(),
        image_generation_run_id=uuid4(),
        storyboard_id=uuid4(),
        storyboard_version=1,
        shot_id=uuid4(),
        shot_sequence=2,
        keyframe_role="FIRST_FRAME",
        compiled_prompt="one frame",
        model=GPT_IMAGE_SNAPSHOT,
        width=1536,
        height=864,
        attempt_number=1,
        provider_configuration_version="test/1",
    )


def test_prompt_is_stable_ordered_and_preserves_invariants() -> None:
    value = intent()
    a = compile_prompt(value)
    b = compile_prompt(value)
    assert a.prompt_hash == b.prompt_hash
    assert (
        a.prompt.index("OUTPUT PURPOSE")
        < a.prompt.index("VISUAL STYLE")
        < a.prompt.index("CHARACTERS")
    )
    assert (
        "Exactly 1 visible characters" in a.prompt
        and "brown skin" in a.prompt
        and "hand on mug" in a.prompt
    )
    assert "negative_prompt" not in a.prompt


def test_last_frame_uses_end_pose() -> None:
    assert "mug raised" in compile_prompt(intent(KeyframeRole.LAST_FRAME)).prompt


def test_optional_compaction_and_required_overflow() -> None:
    value = intent().model_copy(update={"positive_constraints": ["ornate " * 20]})
    assert compile_prompt(value, limit=1_000).diagnostics == ["removed_optional_detail"]
    with pytest.raises(PromptTooLong):
        compile_prompt(intent(), limit=10)


def test_dimension_rules() -> None:
    validate_dimensions(1536, 864)
    with pytest.raises(ValueError):
        validate_dimensions(1537, 864)


@pytest.mark.asyncio
async def test_fake_bytes_are_deterministic_and_validate() -> None:
    first = DeterministicFakeImageProvider()
    second = DeterministicFakeImageProvider()
    a = await first.generate(request())
    b = await second.generate(request())
    assert a.image_base64 == b.image_base64
    result = validate_base64_image(
        a.image_base64, expected_format=a.output_format, width=1536, height=864
    )
    assert result.report.valid and result.report.sha256


@pytest.mark.asyncio
async def test_corrupt_and_wrong_dimensions_are_rejected() -> None:
    with pytest.raises(ValueError):
        validate_base64_image("not base64", expected_format="png", width=16, height=16)  # type: ignore[arg-type]
    bad = await DeterministicFakeImageProvider(corrupt=True).generate(request())
    with pytest.raises(ValueError):
        validate_base64_image(
            bad.image_base64, expected_format=bad.output_format, width=1536, height=864
        )
    wrong = await DeterministicFakeImageProvider(wrong_dimensions=True).generate(request())
    with pytest.raises(ValueError):
        validate_base64_image(
            wrong.image_base64, expected_format=wrong.output_format, width=1536, height=864
        )
