from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

ContractT = TypeVar("ContractT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    content: bytes
    media_type: str
    provider: str
    request_id: str
    metadata: dict[str, Any]


class StructuredReasoner(Protocol):
    async def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        output_type: type[ContractT],
        idempotency_key: str,
    ) -> ContractT: ...


class ImageGenerator(Protocol):
    async def generate(
        self,
        *,
        prompt: str,
        references: tuple[bytes, ...],
        seed: int | None,
        idempotency_key: str,
    ) -> ProviderArtifact: ...


class VideoGenerator(Protocol):
    async def generate(
        self,
        *,
        image: bytes,
        prompt: str,
        duration_seconds: float,
        references: tuple[bytes, ...],
        idempotency_key: str,
    ) -> ProviderArtifact: ...


class VoiceGenerator(Protocol):
    async def generate(
        self,
        *,
        text: str,
        voice: str,
        idempotency_key: str,
    ) -> ProviderArtifact: ...
