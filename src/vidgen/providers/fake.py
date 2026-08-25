from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import wave
import zlib
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from vidgen.providers.base import ContractT, ProviderArtifact


def _request_id(kind: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{idempotency_key}".encode()).hexdigest()[:24]
    return f"fake_{kind}_{digest}"


class FakeStructuredReasoner:
    """Validates configured fixture payloads as if they came from a structured-output LLM."""

    def __init__(self, responses: Mapping[type[BaseModel], dict[str, Any]]) -> None:
        self.responses = responses

    async def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        output_type: type[ContractT],
        idempotency_key: str,
    ) -> ContractT:
        del instructions, input_text, idempotency_key
        if output_type not in self.responses:
            raise KeyError(f"no fake response registered for {output_type.__name__}")
        return output_type.model_validate(self.responses[output_type])


class FakeImageGenerator:
    async def generate(
        self,
        *,
        prompt: str,
        references: tuple[bytes, ...] = (),
        seed: int | None = None,
        idempotency_key: str,
    ) -> ProviderArtifact:
        material = json.dumps(
            {
                "prompt": prompt,
                "references": [hashlib.sha256(item).hexdigest() for item in references],
                "seed": seed,
                "idempotency_key": idempotency_key,
            },
            sort_keys=True,
        ).encode()
        color = hashlib.sha256(material).digest()[:3]
        return ProviderArtifact(
            content=_png(color),
            media_type="image/png",
            provider="fake",
            request_id=_request_id("image", idempotency_key),
            metadata={"seed": seed, "width": 1, "height": 1},
        )


class FakeVideoGenerator:
    async def generate(
        self,
        *,
        image: bytes,
        prompt: str,
        duration_seconds: float,
        references: tuple[bytes, ...] = (),
        idempotency_key: str,
    ) -> ProviderArtifact:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        manifest = {
            "format": "vidgen-fake-video-v1",
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "reference_sha256": [hashlib.sha256(item).hexdigest() for item in references],
        }
        return ProviderArtifact(
            content=json.dumps(manifest, sort_keys=True).encode(),
            media_type="application/x-vidgen-fake-video",
            provider="fake",
            request_id=_request_id("video", idempotency_key),
            metadata={"duration_seconds": duration_seconds},
        )


class FakeVoiceGenerator:
    async def generate(
        self,
        *,
        text: str,
        voice: str,
        idempotency_key: str,
    ) -> ProviderArtifact:
        duration = max(0.25, len(text.split()) / 2.5)
        return ProviderArtifact(
            content=_wav(duration, idempotency_key),
            media_type="audio/wav",
            provider="fake",
            request_id=_request_id("voice", idempotency_key),
            metadata={"duration_seconds": duration, "voice": voice},
        )


def _png(rgb: bytes) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + rgb)
    return signature + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def _wav(duration_seconds: float, key: str) -> bytes:
    sample_rate = 8_000
    frame_count = int(sample_rate * duration_seconds)
    frequency = 220 + int(hashlib.sha256(key.encode()).hexdigest()[:2], 16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = int(2_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", value))
        output.writeframes(bytes(frames))
    return buffer.getvalue()
