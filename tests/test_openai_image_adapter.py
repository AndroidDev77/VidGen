from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.image_generation.openai_image import OpenAIImageProvider, UnknownProviderOutcome
from services.image_generation.providers import GPT_IMAGE_SNAPSHOT
from vidgen.contracts.image_generation import ImageProviderRequest, ImageReferenceBinding


class Images:
    def __init__(self) -> None:
        self.generated = None
        self.edited = None

    def generate(self, **kwargs: object) -> object:
        self.generated = kwargs
        return SimpleNamespace(
            id="img_gen",
            data=[SimpleNamespace(b64_json=base64.b64encode(b"png").decode())],
            usage=None,
        )

    def edit(self, **kwargs: object) -> object:
        self.edited = kwargs
        return SimpleNamespace(
            id="img_edit",
            data=[SimpleNamespace(b64_json=base64.b64encode(b"png").decode())],
            usage=SimpleNamespace(model_dump=lambda: {"output_tokens": 10}),
        )


def request(references: list[ImageReferenceBinding] | None = None) -> ImageProviderRequest:
    return ImageProviderRequest(
        application_idempotency_key="stable",
        project_id=uuid4(),
        image_generation_run_id=uuid4(),
        storyboard_id=uuid4(),
        storyboard_version=1,
        shot_id=uuid4(),
        shot_sequence=0,
        keyframe_role="FIRST_FRAME",
        compiled_prompt="one still",
        references=references or [],
        model=GPT_IMAGE_SNAPSHOT,
        width=1536,
        height=864,
        attempt_number=1,
        provider_configuration_version="test/1",
    )


@pytest.mark.asyncio
async def test_generate_mapping_has_no_input_fidelity() -> None:
    images = Images()
    result = await OpenAIImageProvider(SimpleNamespace(images=images)).generate(request())
    assert images.generated == {
        "model": GPT_IMAGE_SNAPSHOT,
        "prompt": "one still",
        "size": "1536x864",
        "quality": "medium",
        "output_format": "png",
        "background": "opaque",
        "n": 1,
    }
    assert result.provider_request_id == "img_gen" and result.model_snapshot == GPT_IMAGE_SNAPSHOT


@pytest.mark.asyncio
async def test_edit_mapping_uses_verified_reference_bytes() -> None:
    images = Images()
    result = await OpenAIImageProvider(SimpleNamespace(images=images)).generate(
        request(), (b"reference",)
    )
    assert images.edited is not None and images.edited["image"] == [b"reference"]
    assert "input_fidelity" not in images.edited and result.usage == {"output_tokens": 10}


@pytest.mark.asyncio
async def test_ambiguous_timeout_is_not_reported_as_pre_acceptance_failure() -> None:
    class APITimeoutError(Exception):
        pass

    class TimeoutImages:
        def generate(self, **kwargs: object) -> object:
            raise APITimeoutError("unknown acceptance")

    with pytest.raises(UnknownProviderOutcome):
        await OpenAIImageProvider(SimpleNamespace(images=TimeoutImages())).generate(request())


def test_adapter_disables_sdk_retries() -> None:
    class ConfigurableClient:
        def __init__(self) -> None:
            self.max_retries: int | None = None

        def with_options(self, *, max_retries: int):
            self.max_retries = max_retries
            return self

    client = ConfigurableClient()
    provider = OpenAIImageProvider(client)
    assert provider.client is client
    assert client.max_retries == 0
