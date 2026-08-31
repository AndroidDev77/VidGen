from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4


class BlobStore(Protocol):
    def put_if_absent(self, key: str, content: bytes) -> bool: ...

    def put_file_if_absent(self, key: str, source: Path) -> bool: ...

    def read(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def copy_to(self, key: str, destination: Path) -> None: ...

    def signed_read_url(self, key: str, expires_in_seconds: int = 900) -> str: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class RangedBlobStore(Protocol):
    """A store that can serve a byte window without materialising the blob.

    Declared separately from :class:`BlobStore` so a future backend that cannot
    do this is still a valid store: the T25 resumable uploader checks for this
    capability with ``isinstance`` and falls back to a bounded temporary file
    when it is absent, rather than reading a multi-gigabyte MP4 into memory.
    """

    def read_range(self, key: str, start: int, length: int) -> bytes: ...


class FilesystemBlobStore:
    """Atomic local blob store with HMAC-signed read URLs for development and tests."""

    def __init__(
        self, root: Path, signing_secret: bytes, clock: Callable[[], float] = time.time
    ) -> None:
        self.root = root.resolve()
        self.signing_secret = signing_secret
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("blob key escapes storage root")
        return path

    def put_if_absent(self, key: str, content: bytes) -> bool:
        destination = self._path(key)
        if destination.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.upload")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
                return True
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)

    def put_file_if_absent(self, key: str, source: Path) -> bool:
        destination = self._path(key)
        if destination.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.upload")
        try:
            with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            try:
                os.link(temporary, destination)
                return True
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def read_range(self, key: str, start: int, length: int) -> bytes:
        """Return at most ``length`` bytes from ``start``.

        A short read at the end of the file is returned as-is; the caller
        compares against the total size it already knows.
        """
        if start < 0 or length <= 0:
            raise ValueError("a ranged read needs a nonnegative start and a positive length")
        with self._path(key).open("rb") as stream:
            stream.seek(start)
            return stream.read(length)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def copy_to(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._path(key).open("rb") as input_stream, destination.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)

    def _signature(self, key: str, expires: int) -> str:
        message = f"{key}\n{expires}".encode()
        return hmac.new(self.signing_secret, message, hashlib.sha256).hexdigest()

    def signed_read_url(self, key: str, expires_in_seconds: int = 900) -> str:
        if expires_in_seconds <= 0:
            raise ValueError("expiry must be positive")
        if not self.exists(key):
            raise FileNotFoundError(key)
        expires = int(self.clock()) + expires_in_seconds
        signature = self._signature(key, expires)
        return f"vidgen-file://blob/{quote(key)}?expires={expires}&signature={signature}"

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def read_signed_url(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "vidgen-file" or parsed.netloc != "blob":
            raise ValueError("unsupported signed URL")
        key = unquote(parsed.path.lstrip("/"))
        query = parse_qs(parsed.query)
        try:
            expires = int(query["expires"][0])
            supplied = query["signature"][0]
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError("invalid signed URL") from error
        if self.clock() >= expires:
            raise PermissionError("signed URL expired")
        expected = self._signature(key, expires)
        if not hmac.compare_digest(supplied, expected):
            raise PermissionError("invalid signed URL signature")
        return self.read(key)
