"""Bounded streaming download of ephemeral provider output URLs."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True, slots=True)
class DownloadedVideo:
    path: Path
    sha256: str
    byte_size: int
    media_type: str


async def download_video(
    url: str,
    *,
    max_bytes: int = 512 * 1024 * 1024,
    total_timeout_seconds: float = 300,
    client: httpx.AsyncClient | None = None,
) -> DownloadedVideo:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return _copy_local(Path(parsed.path), max_bytes=max_bytes)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("unsupported provider output transport")
    owns_client = client is None
    transport = client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15),
        follow_redirects=False,
    )
    temporary = NamedTemporaryFile(prefix="vidgen-video-", suffix=".mp4", delete=False)
    path = Path(temporary.name)
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    try:
        async with transport.stream("GET", url) as response:
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if media_type not in {"video/mp4", "application/mp4"}:
                raise ValueError("provider output has unsupported Content-Type")
            async for chunk in response.aiter_bytes(1024 * 1024):
                if time.monotonic() - started > total_timeout_seconds:
                    raise TimeoutError("provider output total download timeout")
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("provider output exceeds configured maximum byte size")
                digest.update(chunk)
                temporary.write(chunk)
        temporary.flush()
        temporary.close()
        return DownloadedVideo(path, digest.hexdigest(), size, media_type)
    except BaseException:
        temporary.close()
        await __import__("asyncio").to_thread(path.unlink, missing_ok=True)
        raise
    finally:
        if owns_client:
            await transport.aclose()


def _copy_local(source: Path, *, max_bytes: int) -> DownloadedVideo:
    if not source.is_file():
        raise FileNotFoundError("fake provider output expired")
    temporary = NamedTemporaryFile(prefix="vidgen-video-", suffix=".mp4", delete=False)
    path = Path(temporary.name)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("provider output exceeds configured maximum byte size")
                digest.update(chunk)
                temporary.write(chunk)
        temporary.close()
        return DownloadedVideo(path, digest.hexdigest(), size, "video/mp4")
    except BaseException:
        temporary.close()
        path.unlink(missing_ok=True)
        raise
