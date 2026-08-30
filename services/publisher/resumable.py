"""The YouTube resumable upload driver.

The protocol is simple and the failure modes are not, so the rules this module
enforces are worth stating plainly:

* **The session is persisted before a single media byte is sent.** A worker
  killed after ``videos.insert`` but before the first chunk must be able to
  resume, not start again.
* **Only a server-confirmed offset advances the checkpoint.** A ``308`` carries
  the last byte YouTube actually holds; a lost response carries nothing. After
  any interruption the driver *asks*, and trusts the answer over its own
  bookkeeping.
* **Byte zero is never revisited while a session can be resumed.** The only
  paths that create a second session are an expired session that provably
  created no video, and an explicit new publication identity.
* **The whole file is never in memory.** Chunks come from a
  :class:`~vidgen.storage.blob.RangedBlobStore` window, or - only when the
  configured store cannot serve ranges - from a bounded temporary file that is
  deleted on durable completion or terminal failure.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from services.publisher import youtube as capabilities
from services.publisher.contracts import (
    ChunkSource,
    UploadStatus,
    YouTubeProvider,
    YouTubeProviderError,
)
from services.publisher.credentials import SecretValue
from services.publisher.providers import with_transport_retries
from vidgen.contracts.publication import PublicationFailureCode
from vidgen.storage.blob import BlobStore, RangedBlobStore


class BlobChunkSource:
    """A window onto a content-addressed blob, served by ranged reads."""

    def __init__(self, store: RangedBlobStore, key: str, byte_size: int, media_type: str) -> None:
        self._store = store
        self._key = key
        self._byte_size = byte_size
        self._media_type = media_type

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def media_type(self) -> str:
        return self._media_type

    def read_range(self, start: int, length: int) -> bytes:
        if start >= self._byte_size:
            return b""
        return self._store.read_range(self._key, start, min(length, self._byte_size - start))


class TemporaryFileChunkSource:
    """A bounded staging copy, for a store that cannot serve byte ranges.

    Used only as a fallback. The file is deleted by :meth:`close`, which the
    uploader calls on durable completion *and* on terminal failure, so an
    interrupted publication does not leave a multi-gigabyte file on a worker's
    disposable disk.
    """

    def __init__(self, store: BlobStore, key: str, byte_size: int, media_type: str) -> None:
        handle, path = tempfile.mkstemp(prefix="vidgen-publish-", suffix=".mp4")
        os.close(handle)
        self._path = Path(path)
        try:
            store.copy_to(key, self._path)
        except BaseException:
            self._path.unlink(missing_ok=True)
            raise
        self._byte_size = byte_size
        self._media_type = media_type

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def media_type(self) -> str:
        return self._media_type

    @property
    def path(self) -> Path:
        return self._path

    def read_range(self, start: int, length: int) -> bytes:
        with self._path.open("rb") as stream:
            stream.seek(start)
            return stream.read(length)

    def close(self) -> None:
        self._path.unlink(missing_ok=True)

    def __enter__(self) -> TemporaryFileChunkSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def chunk_source_for(store: BlobStore, *, key: str, byte_size: int, media_type: str) -> ChunkSource:
    """Prefer ranged streaming; stage a bounded temporary file only if forced."""
    if isinstance(store, RangedBlobStore):
        return BlobChunkSource(store, key, byte_size, media_type)
    return TemporaryFileChunkSource(store, key, byte_size, media_type)


def plan_chunks(total_bytes: int, chunk_bytes: int, *, start: int = 0) -> list[tuple[int, int]]:
    """The deterministic ``(offset, length)`` plan for the remaining bytes.

    Every chunk but the last is exactly ``chunk_bytes``, which is a multiple of
    256 KiB, so the plan is a pure function of the total size, the chunk size and
    the starting offset. Two workers resuming the same upload send identical
    ranges.
    """
    if total_bytes <= 0:
        raise ValueError("a resumable upload needs a positive total size")
    if start < 0 or start > total_bytes:
        raise ValueError("the starting offset must lie within the total size")
    size = capabilities.normalize_chunk_bytes(chunk_bytes)
    plan: list[tuple[int, int]] = []
    offset = start
    while offset < total_bytes:
        length = min(size, total_bytes - offset)
        plan.append((offset, length))
        offset += length
    return plan


#: Failures that must stop the drive rather than be smoothed over as progress.
#: Each one needs something outside the uploader to change - the quota clock,
#: a reconnection, a wider consent - before another byte is worth sending.
_PARKING_FAILURE_CODES = frozenset(
    {
        PublicationFailureCode.QUOTA_EXCEEDED,
        PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED,
        PublicationFailureCode.INVALID_GRANT,
        PublicationFailureCode.INSUFFICIENT_SCOPE,
        PublicationFailureCode.AUTHENTICATION_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """What one drive of the uploader achieved."""

    confirmed_offset: int
    completed: bool
    video_id: str | None = None
    #: True when the session is gone and the outcome could not be established.
    ambiguous: bool = False
    #: True when the session is gone and no video was created.
    expired: bool = False
    quota_units: int = 0
    retry_count: int = 0
    last_response_code: int | None = None


class ResumableUploader:
    """Drives one resumable upload from a confirmed offset to completion."""

    def __init__(
        self,
        provider: YouTubeProvider,
        *,
        chunk_bytes: int = capabilities.DEFAULT_CHUNK_BYTES,
        on_confirmed: Callable[[int, int | None], None] | None = None,
        max_chunks_per_drive: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self.chunk_bytes = capabilities.normalize_chunk_bytes(chunk_bytes)
        #: Bounds one drive so the Temporal activity that calls it has a
        #: predictable duration. Progress is durable at every confirmed offset,
        #: so stopping early costs nothing: the next drive resumes from there.
        self.max_chunks_per_drive = max_chunks_per_drive
        #: Called after every server-confirmed offset so the caller can commit
        #: the checkpoint transactionally. This is what makes the upload
        #: restartable rather than merely retryable.
        self.on_confirmed = on_confirmed or (lambda offset, code: None)
        self.clock = clock

    async def confirm_offset(
        self, *, access_token: SecretValue, upload_uri: SecretValue, total_bytes: int
    ) -> UploadStatus:
        """Ask YouTube what it actually holds. The only trustworthy offset."""
        status, _ = await with_transport_retries(
            "videos.insert.status",
            lambda: self.provider.query_upload_status(
                access_token=access_token, upload_uri=upload_uri, total_bytes=total_bytes
            ),
        )
        return status

    async def drive(
        self,
        *,
        access_token: SecretValue,
        upload_uri: SecretValue,
        source: ChunkSource,
        total_bytes: int,
        start_offset: int,
        already_completed_video_id: str | None = None,
    ) -> UploadOutcome:
        """Upload from ``start_offset`` to the end, or explain why it stopped."""
        if already_completed_video_id:
            return UploadOutcome(
                confirmed_offset=total_bytes,
                completed=True,
                video_id=already_completed_video_id,
            )
        quota = 0
        retries = 0
        offset = start_offset
        last_code: int | None = None

        # Never trust a local checkpoint at the start of a resume: a previous
        # worker may have been killed after YouTube accepted bytes it never
        # acknowledged.
        if offset > 0:
            status = await self.confirm_offset(
                access_token=access_token, upload_uri=upload_uri, total_bytes=total_bytes
            )
            if status.expired:
                return UploadOutcome(
                    confirmed_offset=offset,
                    completed=False,
                    expired=True,
                    last_response_code=status.call.http_status,
                )
            if status.completed and status.video_id:
                self.on_confirmed(total_bytes, status.call.http_status)
                return UploadOutcome(
                    confirmed_offset=total_bytes,
                    completed=True,
                    video_id=status.video_id,
                    quota_units=status.call.quota_units,
                    last_response_code=status.call.http_status,
                )
            offset = min(status.confirmed_offset, total_bytes)
            last_code = status.call.http_status
            self.on_confirmed(offset, last_code)

        sent = 0
        for chunk_start, length in plan_chunks(total_bytes, self.chunk_bytes, start=offset):
            if self.max_chunks_per_drive is not None and sent >= self.max_chunks_per_drive:
                # Stop cleanly with the confirmed offset persisted. The next
                # drive continues from exactly here.
                return UploadOutcome(
                    confirmed_offset=offset,
                    completed=False,
                    quota_units=quota,
                    retry_count=retries,
                    last_response_code=last_code,
                )
            sent += 1
            chunk = source.read_range(chunk_start, length)
            if len(chunk) != length:
                raise YouTubeProviderError(
                    PublicationFailureCode.MISSING_FINAL_ASSET,
                    "the final render is shorter than its recorded byte size; "
                    "the stored asset no longer matches its provenance",
                )
            try:
                status, spent = await with_transport_retries(
                    "videos.insert.chunk",
                    lambda chunk=chunk, chunk_start=chunk_start: self.provider.upload_chunk(  # type: ignore[misc]
                        access_token=access_token,
                        upload_uri=upload_uri,
                        chunk=chunk,
                        start=chunk_start,
                        total_bytes=total_bytes,
                    ),
                )
                retries += spent
            except YouTubeProviderError as error:
                if error.code is PublicationFailureCode.EXPIRED_RESUMABLE_SESSION:
                    # A session that vanished while the *final* chunk was in
                    # flight is the genuinely ambiguous case: YouTube may have
                    # assembled the video before the session was collected, and
                    # there is no longer anything to ask.
                    was_final = chunk_start + length >= total_bytes
                    return UploadOutcome(
                        confirmed_offset=offset,
                        completed=False,
                        expired=True,
                        ambiguous=was_final,
                        quota_units=quota,
                        retry_count=retries,
                        last_response_code=error.http_status,
                    )
                # The bytes may or may not have landed. Ask, rather than
                # assuming either way; the answer decides everything.
                return await self._resolve_after_interruption(
                    access_token=access_token,
                    upload_uri=upload_uri,
                    total_bytes=total_bytes,
                    offset=offset,
                    quota=quota,
                    retries=retries,
                    error=error,
                )

            quota += status.call.quota_units
            last_code = status.call.http_status
            if status.completed and status.video_id:
                self.on_confirmed(total_bytes, last_code)
                return UploadOutcome(
                    confirmed_offset=total_bytes,
                    completed=True,
                    video_id=status.video_id,
                    quota_units=quota,
                    retry_count=retries,
                    last_response_code=last_code,
                )
            if status.expired:
                # The adapter reports a gone session as a status rather than an
                # error, so this is the path a real 410 takes. A session that
                # vanished while the *final* chunk was in flight is ambiguous
                # for exactly the same reason as the raised form: YouTube may
                # have assembled the video before collecting the session, and
                # there is nothing left to ask. Without this the caller would
                # see "expired with nothing confirmed" and start a second
                # upload, which is how a duplicate video gets created.
                return UploadOutcome(
                    confirmed_offset=offset,
                    completed=False,
                    expired=True,
                    ambiguous=chunk_start + length >= total_bytes,
                    quota_units=quota,
                    retry_count=retries,
                    last_response_code=last_code,
                )
            # A 308 whose Range disagrees with the chunk we just sent is the
            # server correcting us. Take its number and continue from there,
            # clamped: a server can never hold more bytes than we declared.
            offset = min(max(offset, status.confirmed_offset), total_bytes)
            self.on_confirmed(offset, last_code)

        # Every planned chunk was accepted without a completion response. That
        # is not proof of success: ask once more before deciding.
        status = await self.confirm_offset(
            access_token=access_token, upload_uri=upload_uri, total_bytes=total_bytes
        )
        if status.completed and status.video_id:
            self.on_confirmed(total_bytes, status.call.http_status)
            return UploadOutcome(
                confirmed_offset=total_bytes,
                completed=True,
                video_id=status.video_id,
                quota_units=quota,
                retry_count=retries,
                last_response_code=status.call.http_status,
            )
        return UploadOutcome(
            confirmed_offset=min(max(offset, status.confirmed_offset), total_bytes),
            completed=False,
            ambiguous=status.expired,
            expired=status.expired,
            quota_units=quota,
            retry_count=retries,
            last_response_code=status.call.http_status,
        )

    async def _resolve_after_interruption(
        self,
        *,
        access_token: SecretValue,
        upload_uri: SecretValue,
        total_bytes: int,
        offset: int,
        quota: int,
        retries: int,
        error: YouTubeProviderError,
    ) -> UploadOutcome:
        """Establish the truth after a chunk failed mid-flight.

        The offset is settled first, always: whatever else is wrong, bytes the
        server has confirmed must not be sent again. Then the outcome:

        * the upload actually completed - take the video ID;
        * the session is gone and we cannot tell - ambiguous, and the caller
          escalates rather than re-uploading;
        * the server simply has a different offset - resume from it, unless the
          interrupting failure was one that has to park the publication (an
          exhausted quota, a revoked grant, an insufficient scope). Reporting
          one of those as "still uploading" would have the workflow re-drive
          into the same refusal instead of waiting.
        """
        try:
            status = await self.confirm_offset(
                access_token=access_token, upload_uri=upload_uri, total_bytes=total_bytes
            )
        except YouTubeProviderError as probe_error:
            return UploadOutcome(
                confirmed_offset=offset,
                completed=False,
                ambiguous=True,
                expired=probe_error.code is PublicationFailureCode.EXPIRED_RESUMABLE_SESSION,
                quota_units=quota,
                retry_count=retries,
                last_response_code=probe_error.http_status or error.http_status,
            )
        if status.expired:
            return UploadOutcome(
                confirmed_offset=offset,
                completed=False,
                ambiguous=True,
                expired=True,
                quota_units=quota,
                retry_count=retries,
                last_response_code=status.call.http_status,
            )
        if status.completed and status.video_id:
            self.on_confirmed(total_bytes, status.call.http_status)
            return UploadOutcome(
                confirmed_offset=total_bytes,
                completed=True,
                video_id=status.video_id,
                quota_units=quota,
                retry_count=retries,
                last_response_code=status.call.http_status,
            )
        confirmed = min(max(offset, status.confirmed_offset), total_bytes)
        # Committed before the failure is re-raised, so a parked publication
        # still resumes from the server-confirmed offset rather than byte zero.
        self.on_confirmed(confirmed, status.call.http_status)
        if error.code in _PARKING_FAILURE_CODES:
            raise error
        return UploadOutcome(
            confirmed_offset=confirmed,
            completed=False,
            quota_units=quota,
            retry_count=retries,
            last_response_code=status.call.http_status,
        )


def release(source: ChunkSource) -> None:
    """Delete a staged temporary file, if this source has one."""
    close = getattr(source, "close", None)
    if callable(close):
        with contextlib.suppress(OSError):
            close()
