"""Deterministic, credential-free synthetic narration."""

from __future__ import annotations

import hashlib
import math
import struct
import wave
from pathlib import Path

from vidgen.contracts.narration import NarrationProviderRequest, NarrationProviderResult


class FakeNarrationProvider:
    name = "fake"

    async def generate(
        self, request: NarrationProviderRequest, destination: Path
    ) -> NarrationProviderResult:
        words = len(request.text.split())
        frames = max(2400, round(words / (150 * request.speed) * 60 * 48_000))
        seed = int(hashlib.sha256(request.text.encode()).hexdigest()[:4], 16)
        with wave.open(str(destination), "wb") as wav:
            wav.setparams((1, 2, 48_000, frames, "NONE", "not compressed"))
            for i in range(frames):
                envelope = min(1.0, i / 480) * min(1.0, (frames - i) / 480)
                sample = int(
                    6000 * envelope * math.sin(2 * math.pi * (180 + seed % 120) * i / 48_000)
                )
                wav.writeframesraw(struct.pack("<h", sample))
        return NarrationProviderResult(
            provider="fake",
            model=request.model,
            provider_request_id=f"fake-{request.idempotency_key[:24]}",
            attempt_number=request.attempt_number,
            content_type="audio/wav",
            audio_format="wav",
            byte_size=destination.stat().st_size,  # noqa: ASYNC240
            usage={"characters": len(request.text)},
            provider_duration_seconds=frames / 48_000,
            idempotency_key=request.idempotency_key,
        )
