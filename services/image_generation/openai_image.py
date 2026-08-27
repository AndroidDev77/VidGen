"""OpenAI Image API adapter isolated from canonical pipeline contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from services.image_generation.providers import GPT_IMAGE_SNAPSHOT, validate_dimensions
from vidgen.contracts.image_generation import ImageProviderRequest, ImageProviderResult


class UnknownProviderOutcome(RuntimeError):
    """Transport ended after submission and provider creation cannot be excluded."""


class OpenAIImageProvider:
    name = "openai"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def generate(
        self, request: ImageProviderRequest, reference_bytes: tuple[bytes, ...] = ()
    ) -> ImageProviderResult:
        validate_dimensions(
            request.width,
            request.height,
            experimental=bool(request.provider_options.get("experimental_resolution")),
        )
        kwargs: dict[str, Any] = dict(
            model=request.model,
            prompt=request.compiled_prompt,
            size=f"{request.width}x{request.height}",
            quality=request.quality.value,
            output_format=request.output_format.value,
            background=request.background,
            n=1,
        )
        started = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        try:
            if reference_bytes:
                kwargs["image"] = list(reference_bytes)
                response = await loop.run_in_executor(
                    None, lambda: self.client.images.edit(**kwargs)
                )
            else:
                response = await loop.run_in_executor(
                    None, lambda: self.client.images.generate(**kwargs)
                )
        except Exception as exc:
            if type(exc).__name__ in {"APITimeoutError", "ReadTimeout"}:
                raise UnknownProviderOutcome(
                    "image request outcome is unknown; do not retry the same attempt"
                ) from exc
            raise
        elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
        images = getattr(response, "data", [])
        if len(images) != 1 or not getattr(images[0], "b64_json", None):
            raise ValueError("OpenAI returned an invalid image count or missing base64 content")
        usage = getattr(response, "usage", None)
        usage_dict: dict[str, Any] = {}
        if usage is not None and hasattr(usage, "model_dump"):
            usage_dict = usage.model_dump()
        return ImageProviderResult(
            provider=self.name,
            model=request.model,
            model_snapshot=GPT_IMAGE_SNAPSHOT if request.model == GPT_IMAGE_SNAPSHOT else None,
            requested_at=started,
            provider_request_id=getattr(response, "id", None),
            attempt_number=request.attempt_number,
            returned_image_count=1,
            output_format=request.output_format,
            usage={k: int(v) for k, v in usage_dict.items() if isinstance(v, int)},
            response_metadata={},
            latency_ms=elapsed,
            application_idempotency_key=request.application_idempotency_key,
            provider_configuration_version=request.provider_configuration_version,
            image_base64=images[0].b64_json,
        )
