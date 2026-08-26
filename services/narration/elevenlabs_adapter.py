"""Optional streaming ElevenLabs TTS fallback."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import httpx

from vidgen.contracts.narration import NarrationProviderRequest, NarrationProviderResult

from .providers import ELEVENLABS_TTS_URL


class ElevenLabsNarrationProvider:
    name = "elevenlabs"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("ElevenLabs API key is required")
        self.api_key, self.client = api_key, client

    async def generate(
        self, request: NarrationProviderRequest, destination: Path
    ) -> NarrationProviderResult:
        client = self.client or httpx.AsyncClient(timeout=120)
        started = time.monotonic()
        try:
            async with client.stream(
                "POST",
                ELEVENLABS_TTS_URL.format(voice_id=request.voice_id),
                params={"output_format": request.output_format},
                headers={"xi-api-key": self.api_key},
                json={
                    "text": request.text,
                    "model_id": request.model,
                    "voice_settings": {"speed": request.speed},
                },
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
                request_id = response.headers.get("request-id", str(uuid4()))
                media_type = response.headers.get("content-type", "audio/mpeg").split(";")[0]
        finally:
            if self.client is None:
                await client.aclose()
        return NarrationProviderResult(
            provider=self.name,
            model=request.model,
            provider_request_id=request_id,
            attempt_number=request.attempt_number,
            content_type=media_type,
            audio_format=request.output_format,
            byte_size=destination.stat().st_size,  # noqa: ASYNC240
            usage={"characters": len(request.text)},
            provider_duration_seconds=time.monotonic() - started,
            idempotency_key=request.idempotency_key,
        )
