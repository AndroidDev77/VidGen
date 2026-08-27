"""Deterministic network-free PNG provider."""

from __future__ import annotations

import base64
import hashlib
import io
from datetime import UTC, datetime

from PIL import Image, ImageDraw

from vidgen.contracts.image_generation import ImageFormat, ImageProviderRequest, ImageProviderResult


class DeterministicFakeImageProvider:
    name = "fake"

    def __init__(self, *, corrupt: bool = False, wrong_dimensions: bool = False) -> None:
        self.corrupt = corrupt
        self.wrong_dimensions = wrong_dimensions
        self.call_count = 0

    async def generate(
        self, request: ImageProviderRequest, reference_bytes: tuple[bytes, ...] = ()
    ) -> ImageProviderResult:
        self.call_count += 1
        seed = hashlib.sha256(request.application_idempotency_key.encode()).digest()
        width = request.width + 16 if self.wrong_dimensions else request.width
        image = Image.new("RGB", (width, request.height), tuple(seed[:3]))
        draw = ImageDraw.Draw(image)
        x = (request.shot_sequence * 37) % max(1, width - 32)
        fill = tuple(255 - value for value in seed[:3])
        draw.rectangle((x, 8, min(width - 1, x + 24), 32), fill=fill)
        if request.keyframe_role.value == "LAST_FRAME":
            draw.ellipse(
                (width - 40, request.height - 40, width - 8, request.height - 8), fill=fill
            )
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False)
        data = b"corrupt" if self.corrupt else output.getvalue()
        return ImageProviderResult(
            provider=self.name,
            model=request.model,
            requested_at=datetime.now(UTC),
            attempt_number=request.attempt_number,
            returned_image_count=1,
            output_format=ImageFormat.PNG,
            declared_width=width,
            declared_height=request.height,
            response_metadata={"fake": True, "reference_count": len(reference_bytes)},
            latency_ms=0,
            application_idempotency_key=request.application_idempotency_key,
            provider_configuration_version=request.provider_configuration_version,
            image_base64=base64.b64encode(data).decode(),
        )
