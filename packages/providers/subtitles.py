from __future__ import annotations

import hashlib

from vidgen.contracts.subtitles import (
    ProviderSubtitleDownload,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


class FakeSubtitleProvider:
    provider_name = "fake-subtitles"

    def __init__(self, content: bytes | None = None) -> None:
        self.content = content or (
            b"1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n"
            b"2\n00:00:01,100 --> 00:00:02,500\nGeneral Kenobi.\n"
        )
        self.search_calls: list[SubtitleSearchRequest] = []
        self.download_calls: list[str] = []

    async def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        self.search_calls.append(request)
        identity = request.movie_hash or request.imdb_id or request.query or "fixture"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return [
            SubtitleCandidate(
                candidate_id=f"fake_{digest}",
                source_type="provider",
                provider=self.provider_name,
                provider_subtitle_id=digest,
                provider_file_id=1,
                language=request.languages[0],
                subtitle_format="srt",
                file_name="fixture.srt",
                download_count=100,
            )
        ]

    async def download(
        self, candidate: SubtitleCandidate, *, idempotency_key: str
    ) -> ProviderSubtitleDownload:
        self.download_calls.append(idempotency_key)
        request_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return ProviderSubtitleDownload(
            candidate_id=candidate.candidate_id,
            provider=self.provider_name,
            provider_request_id=f"fake_download_{request_id}",
            file_name=candidate.file_name or "fixture.srt",
            media_type="application/x-subrip",
            content=self.content,
        )
