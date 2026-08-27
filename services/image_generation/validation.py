"""Bounded deterministic technical validation for generated images."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageStat, UnidentifiedImageError

from services.image_generation.providers import DEFAULT_LIMITS
from vidgen.contracts.image_generation import (
    ImageFormat,
    ImageValidationDiagnostic,
    ImageValidationReport,
)


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    content: bytes
    report: ImageValidationReport


def validate_base64_image(
    value: str, *, expected_format: ImageFormat, width: int, height: int
) -> ValidatedImage:
    diagnostics: list[ImageValidationDiagnostic] = []
    if len(value.encode()) > DEFAULT_LIMITS.max_base64_bytes:
        raise ValueError("encoded image exceeds configured limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image") from exc
    if len(data) > DEFAULT_LIMITS.max_decoded_bytes:
        raise ValueError("decoded image exceeds configured limit")
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("corrupt or truncated image") from exc
    detected = {"PNG": ImageFormat.PNG, "JPEG": ImageFormat.JPEG, "WEBP": ImageFormat.WEBP}.get(
        image.format or ""
    )
    if detected is None or detected != expected_format:
        raise ValueError("declared and detected image formats differ")
    if image.size != (width, height):
        raise ValueError(f"image dimensions {image.size} do not match {(width, height)}")
    if image.mode not in {"RGB", "RGBA"}:
        raise ValueError(f"unsupported color mode {image.mode}")
    extrema = ImageStat.Stat(image.convert("RGB")).extrema
    if all(high - low < 2 for low, high in extrema):
        diagnostics.append(
            ImageValidationDiagnostic(
                code="near_uniform",
                severity="error",
                message="image has no meaningful pixel variance",
            )
        )
    digest = hashlib.sha256(data).hexdigest()
    valid = not any(d.severity == "error" for d in diagnostics)
    report = ImageValidationReport(
        valid=valid,
        actual_format=detected,
        mime_type=f"image/{detected.value}",
        width=width,
        height=height,
        aspect_ratio=width / height,
        color_mode=image.mode,
        has_alpha="A" in image.getbands(),
        byte_size=len(data),
        sha256=digest,
        diagnostics=diagnostics,
    )
    return ValidatedImage(data, report)
