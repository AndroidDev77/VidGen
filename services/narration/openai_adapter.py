"""Streaming OpenAI speech adapter (no SDK response types cross this boundary)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import uuid4

import httpx

from vidgen.contracts.narration import NarrationProviderRequest, NarrationProviderResult
from vidgen.providers.openai_rate_limit import (
    DEFAULT_BACKOFF,
    RATE_LIMIT_STATUS,
    RateLimitBackoff,
    retry_delay_for_rate_limit,
)

from .providers import OPENAI_FORMATS, OPENAI_SPEECH_URL, OPENAI_TTS_MODEL, OPENAI_VOICES


class OpenAINarrationProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        backoff: RateLimitBackoff = DEFAULT_BACKOFF,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        self.api_key, self.client = api_key, client
        self.backoff = backoff

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
        attempt = 1
        try:
            while True:
                # The audio arrives as a stream, so a rate-limited attempt is
                # retried by re-entering the request rather than by replaying a
                # response somebody else already consumed.
                started = time.monotonic()
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
                    if response.status_code == RATE_LIMIT_STATUS:
                        await response.aread()
                        delay = retry_delay_for_rate_limit(
                            response, attempt=attempt, policy=self.backoff
                        )
                    else:
                        response.raise_for_status()
                        with destination.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                output.write(chunk)
                        request_id = response.headers.get("x-request-id", str(uuid4()))
                        media_type = response.headers.get(
                            "content-type", "application/octet-stream"
                        ).split(";")[0]
                        break
                await asyncio.sleep(delay)
                attempt += 1
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
