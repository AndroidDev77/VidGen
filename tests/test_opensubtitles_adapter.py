from __future__ import annotations

import httpx
import pytest

from services.subtitles.opensubtitles import OpenSubtitlesAdapter
from vidgen.contracts.subtitles import SubtitleSearchRequest


@pytest.mark.asyncio
async def test_hash_first_search_and_authenticated_download() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/subtitles"):
            assert request.url.params["moviehash"] == "a" * 16
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "42",
                            "attributes": {
                                "language": "en",
                                "download_count": 123,
                                "files": [{"file_id": 9, "file_name": "episode.srt"}],
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "jwt"})
        if request.url.path.endswith("/download"):
            assert request.headers["authorization"] == "Bearer jwt"
            return httpx.Response(
                200,
                json={
                    "link": "https://download.test/episode.srt",
                    "file_name": "episode.srt",
                    "remaining": 99,
                },
                headers={"x-request-id": "download-request"},
            )
        return httpx.Response(200, content=b"subtitle bytes")

    client = httpx.AsyncClient(
        base_url="https://api.opensubtitles.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    adapter = OpenSubtitlesAdapter(api_key="key", username="user", password="pass", client=client)
    candidates = await adapter.search(
        SubtitleSearchRequest(
            idempotency_key="search",
            movie_hash="a" * 16,
            byte_size=100,
            query="Episode",
            languages=["en"],
        )
    )
    download = await adapter.download(candidates[0], idempotency_key="download")
    await client.aclose()
    assert candidates[0].provider_file_id == 9
    assert download.content == b"subtitle bytes"
    assert download.remaining_downloads == 99
    assert [request.method for request in requests] == ["GET", "POST", "POST", "GET"]


@pytest.mark.asyncio
async def test_search_falls_back_from_hash_to_query() -> None:
    queries: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        queries.append(str(request.url.query))
        if "moviehash" in request.url.params:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "attributes": {
                            "language": "en",
                            "files": [{"file_id": 2, "file_name": "result.srt"}],
                        },
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.test/api/v1", transport=httpx.MockTransport(handler)
    )
    adapter = OpenSubtitlesAdapter(api_key="key", client=client)
    result = await adapter.search(
        SubtitleSearchRequest(
            idempotency_key="fallback",
            movie_hash="b" * 16,
            query="A Show",
            languages=["en"],
        )
    )
    await client.aclose()
    assert len(queries) == 2
    assert result[0].provider_file_id == 2
