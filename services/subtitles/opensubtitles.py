from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx

from vidgen.contracts.subtitles import (
    ProviderSubtitleDownload,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


class OpenSubtitlesAdapter:
    provider_name = "opensubtitles"

    def __init__(
        self,
        *,
        api_key: str,
        username: str | None = None,
        password: str | None = None,
        user_agent: str = "VidGen v0.1",
        base_url: str = "https://api.opensubtitles.com/api/v1",
        client: httpx.AsyncClient | None = None,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("OpenSubtitles API key is required")
        if (username is None) != (password is None):
            raise ValueError("OpenSubtitles username and password must be configured together")
        self.api_key = api_key
        self.username = username
        self.password = password
        self.user_agent = user_agent
        self.max_retries = max_retries
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=60)
        self._token: str | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        attempts: list[dict[str, str | int]] = []
        common: dict[str, str | int] = {
            "languages": ",".join(request.languages),
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if request.movie_hash:
            attempts.append({**common, "moviehash": request.movie_hash})
        if request.imdb_id:
            attempts.append({**common, "imdb_id": request.imdb_id.removeprefix("tt")})
        if request.query:
            params = {**common, "query": request.query}
            if request.season_number is not None:
                params["season_number"] = request.season_number
            if request.episode_number is not None:
                params["episode_number"] = request.episode_number
            attempts.append(params)
        if not attempts:
            raise ValueError("subtitle search requires a movie hash, IMDb ID, or query")
        for params in attempts:
            response = await self._request("GET", "/subtitles", params=params)
            candidates = self._parse_candidates(_json_object(response))
            if candidates:
                return candidates
        return []

    async def download(
        self, candidate: SubtitleCandidate, *, idempotency_key: str
    ) -> ProviderSubtitleDownload:
        if candidate.provider_file_id is None:
            raise ValueError("OpenSubtitles candidate has no file ID")
        await self._ensure_token()
        response = await self._request(
            "POST",
            "/download",
            json_body={"file_id": candidate.provider_file_id},
        )
        payload = _json_object(response)
        link = payload.get("link")
        if not isinstance(link, str) or not link:
            raise ValueError("OpenSubtitles download response has no link")
        file_response = await self._request("GET", link, include_api_headers=False)
        request_id = response.headers.get("x-request-id") or _stable_id(
            {"idempotency_key": idempotency_key, "payload": payload}
        )
        remaining = payload.get("remaining")
        return ProviderSubtitleDownload(
            candidate_id=candidate.candidate_id,
            provider=self.provider_name,
            provider_request_id=request_id,
            file_name=str(payload.get("file_name") or candidate.file_name or "subtitle.srt"),
            media_type="application/x-subrip",
            content=file_response.content,
            remaining_downloads=remaining
            if isinstance(remaining, int) and remaining >= 0
            else None,
        )

    async def _ensure_token(self) -> None:
        if self._token is not None or self.username is None or self.password is None:
            return
        response = await self._request(
            "POST",
            "/login",
            json_body={"username": self.username, "password": self.password},
            authenticate=False,
        )
        token = _json_object(response).get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("OpenSubtitles login returned no token")
        self._token = token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
        authenticate: bool = True,
        include_api_headers: bool = True,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if include_api_headers:
            headers.update({"Api-Key": self.api_key, "User-Agent": self.user_agent})
        if authenticate and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for attempt in range(self.max_retries):
            response = await self.client.request(
                method, url, headers=headers, params=params, json=json_body
            )
            if response.status_code not in {429, 502, 503, 504}:
                response.raise_for_status()
                return response
            if attempt + 1 == self.max_retries:
                response.raise_for_status()
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            await asyncio.sleep(min(delay, 30))
        raise RuntimeError("unreachable subtitle provider retry state")

    def _parse_candidates(self, payload: dict[str, Any]) -> list[SubtitleCandidate]:
        result: list[SubtitleCandidate] = []
        for raw in payload.get("data", []):
            if not isinstance(raw, dict):
                continue
            attributes = raw.get("attributes")
            if not isinstance(attributes, dict):
                continue
            files = attributes.get("files")
            if not isinstance(files, list):
                continue
            for file_data in files:
                if not isinstance(file_data, dict) or not isinstance(file_data.get("file_id"), int):
                    continue
                file_id = int(file_data["file_id"])
                subtitle_id = str(raw.get("id") or file_id)
                result.append(
                    SubtitleCandidate(
                        candidate_id=f"opensubtitles_{subtitle_id}_{file_id}",
                        source_type="provider",
                        provider=self.provider_name,
                        provider_subtitle_id=subtitle_id,
                        provider_file_id=file_id,
                        language=_optional_string(attributes.get("language")),
                        subtitle_format="srt",
                        hearing_impaired=bool(attributes.get("hearing_impaired")),
                        forced=bool(attributes.get("foreign_parts_only")),
                        release_name=_optional_string(attributes.get("release")),
                        file_name=_optional_string(file_data.get("file_name")),
                        fps=_optional_float(attributes.get("fps")),
                        download_count=_nonnegative_int(attributes.get("download_count")),
                        metadata={
                            "feature_details": attributes.get("feature_details") or {},
                            "ratings": attributes.get("ratings"),
                            "from_trusted": bool(attributes.get("from_trusted")),
                            "machine_translated": bool(attributes.get("machine_translated")),
                            "ai_translated": bool(attributes.get("ai_translated")),
                        },
                    )
                )
        return result


def _json_object(response: httpx.Response) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("subtitle provider returned a non-object response")
    return value


def _stable_id(value: dict[str, object]) -> str:
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]
    return f"opensubtitles_{digest}"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
