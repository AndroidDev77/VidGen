"""Provider boundary and centralized GPT Image configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vidgen.contracts.image_generation import ImageProviderRequest, ImageProviderResult

GPT_IMAGE_MODEL = "gpt-image-2"
GPT_IMAGE_SNAPSHOT = "gpt-image-2-2026-04-21"


@dataclass(frozen=True, slots=True)
class ImageProviderLimits:
    prompt_characters: int = 32_000
    max_references: int = 16
    max_reference_bytes: int = 50 * 1024 * 1024
    max_base64_bytes: int = 64 * 1024 * 1024
    max_decoded_bytes: int = 48 * 1024 * 1024
    min_pixels: int = 256 * 256
    max_pixels: int = 16_777_216
    max_edge: int = 4096
    reliability_max_edge: int = 2048


DEFAULT_LIMITS = ImageProviderLimits()


def validate_dimensions(width: int, height: int, *, experimental: bool = False) -> None:
    if width <= 0 or height <= 0 or width % 16 or height % 16:
        raise ValueError("image dimensions must be positive and divisible by 16")
    ratio = max(width / height, height / width)
    if ratio > 3:
        raise ValueError("image aspect ratio cannot be more extreme than 3:1")
    pixels = width * height
    if not DEFAULT_LIMITS.min_pixels <= pixels <= DEFAULT_LIMITS.max_pixels:
        raise ValueError("image pixel count is outside provider limits")
    if max(width, height) > DEFAULT_LIMITS.max_edge:
        raise ValueError("image edge exceeds provider limit")
    if not experimental and max(width, height) > DEFAULT_LIMITS.reliability_max_edge:
        raise ValueError("resolution exceeds reliability boundary; enable experimental mode")


class ImageGenerationProvider(Protocol):
    name: str

    async def generate(
        self, request: ImageProviderRequest, reference_bytes: tuple[bytes, ...] = ()
    ) -> ImageProviderResult: ...
