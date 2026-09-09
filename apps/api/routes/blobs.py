"""Signed blob-serving endpoint for the filesystem backend.

In cloud deployments the BlobStore returns real Azure SAS URLs, which the
browser opens directly. In local/filesystem mode it returns a
``vidgen-file://`` URL that only the API process can read. The
``/api/v1/blobs/{key}`` endpoint bridges the gap by verifying the HMAC
signature and streaming the content back to the browser.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from apps.api.dependencies import get_blob_store
from vidgen.storage.blob import FilesystemBlobStore

router = APIRouter(prefix="/blobs", tags=["blobs"])

_CHUNK = 256 * 1024  # 256 KB streaming chunks


@router.get("/{key:path}")
def serve_blob(
    key: str,
    expires: int,
    signature: str,
    blob_store: Annotated[object, Depends(get_blob_store)],
    content_type: str = Query(default="application/octet-stream"),
) -> StreamingResponse:
    """Stream a blob file whose HMAC signature has been pre-validated."""
    if not isinstance(blob_store, FilesystemBlobStore):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="blob serving endpoint only available for filesystem backend",
        )
    url = f"vidgen-file://blob/{quote(key)}?expires={expires}&signature={signature}"
    try:
        content = blob_store.read_signed_url(url)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blob not found") from exc

    def _chunks(data: bytes, size: int) -> Iterator[bytes]:
        for offset in range(0, len(data), size):
            yield data[offset : offset + size]

    return StreamingResponse(_chunks(content, _CHUNK), media_type=content_type)
