"""Streaming OpenAI speech adapter (no SDK response types cross this boundary)."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import httpx

from vidgen.contracts.narration import NarrationProviderRequest, NarrationProviderResult

from .providers import OPENAI_FORMATS, OPENAI_SPEECH_URL, OPENAI_TTS_MODEL, OPENAI_VOICES


class OpenAINarrationProvider:
    name = "openai"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        self.api_key, self.client = api_key, client

    async def generate(
        self, request: NarrationProviderRequest, destination: Path
    ) -> NarrationProviderResult:
        if (
            request.model != OPENAI_TTS_MODEL
            or request.voice_id not in OPENAI_VOICES
            or request.output_format not in OPENAI_FORMATS
        ):
            raise ValueError("unsupported OpenAI narration model, voice, or format")
        client = self.client or httpx.AsyncClient(timeout=120)
        started = time.monotonic()
        try:
            async with client.stream(
                "POST",
                OPENAI_SPEECH_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Idempotency-Key": request.idempotency_key,
                },
                json={
                    "model": request.model,
                    "voice": request.voice_id,
                    "input": request.text,
                    "instructions": request.speaking_instructions,
                    "response_format": request.output_format,
                    "speed": request.speed,
                },
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
                request_id = response.headers.get("x-request-id", str(uuid4()))
                media_type = response.headers.get("content-type", "application/octet-stream").split(
                    ";"
                )[0]
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
            response_metadata={},
            provider_duration_seconds=time.monotonic() - started,
            idempotency_key=request.idempotency_key,
        )
