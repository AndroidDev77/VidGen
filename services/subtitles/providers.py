from __future__ import annotations

from pathlib import Path
from typing import Protocol

from vidgen.contracts.subtitles import (
    ProviderSubtitleDownload,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


class SubtitleProvider(Protocol):
    provider_name: str

    async def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]: ...

    async def download(
        self, candidate: SubtitleCandidate, *, idempotency_key: str
    ) -> ProviderSubtitleDownload: ...


def sidecar_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in {"srt", "vtt", "ass", "ssa"}:
        raise ValueError(f"unsupported sidecar subtitle format: {path.suffix}")
    return suffix
