"""Azure Blob Storage adapter for the content-addressed :class:`BlobStore` protocol.

The deployed Container Apps have no shared, durable filesystem: the API, the
Temporal worker and every finite job run in separate containers with disposable
local disks. Canonical assets therefore have to live in Blob Storage, and the
content-addressed identity the :class:`~vidgen.storage.asset_service.AssetService`
depends on has to survive that move unchanged.

Two properties are load bearing and are preserved exactly:

* ``put_if_absent``/``put_file_if_absent`` never overwrite an existing blob and
  report whether this call created it, so a retried activity deduplicates
  instead of rewriting immutable provenance. The conditional is enforced by the
  service with ``If-None-Match: *`` rather than by a read-then-write race.
* Reads and writes stream, so a multi-gigabyte source video never has to be
  materialised in container memory.

Access is by managed identity. No account key, connection string or SAS token
is read from configuration, and signed read URLs are minted from a
*user delegation* key, which is itself derived from the caller's identity and
expires. The SDK imports stay inside this module so a filesystem deployment
never needs the Azure packages installed.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol
from urllib.parse import quote
from uuid import uuid4

if TYPE_CHECKING:  # pragma: no cover - import-only, keeps the SDK optional
    from azure.core.credentials import TokenCredential
    from azure.storage.blob import BlobServiceClient

#: A user delegation key is requested for this long and reused until it is
#: within ``_DELEGATION_REFRESH`` of expiring. Short enough that a revoked
#: identity stops minting URLs quickly, long enough that listing a project's
#: assets does not issue one request per asset.
_DELEGATION_LIFETIME = dt.timedelta(hours=1)
_DELEGATION_REFRESH = dt.timedelta(minutes=5)


class _Clock(Protocol):
    def __call__(self) -> dt.datetime: ...


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class AzureBlobStore:
    """A :class:`~vidgen.storage.blob.BlobStore` backed by one Blob container."""

    def __init__(
        self,
        *,
        account_url: str,
        container: str,
        credential: TokenCredential | None = None,
        service_client: BlobServiceClient | None = None,
        clock: _Clock = _utc_now,
        signed_url_max_seconds: int = 3600,
    ) -> None:
        if service_client is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            service_client = BlobServiceClient(
                account_url=account_url,
                credential=credential if credential is not None else DefaultAzureCredential(),
            )
        self._service = service_client
        self._container_name = container
        self._container = service_client.get_container_client(container)
        self._clock = clock
        self._signed_url_max_seconds = signed_url_max_seconds
        self._delegation_key: Any | None = None
        self._delegation_expiry: dt.datetime | None = None

    @property
    def container_name(self) -> str:
        return self._container_name

    # -- writes ---------------------------------------------------------------

    def put_if_absent(self, key: str, content: bytes) -> bool:
        return self._upload(key, content, length=len(content))

    def put_file_if_absent(self, key: str, source: Path) -> bool:
        with source.open("rb") as stream:
            return self._upload(key, stream, length=source.stat().st_size)

    def _upload(self, key: str, data: bytes | IO[bytes], *, length: int) -> bool:
        from azure.core.exceptions import ResourceExistsError

        blob = self._container.get_blob_client(key)
        try:
            # ``overwrite=False`` becomes an ``If-None-Match: *`` precondition,
            # so two concurrent activities storing the same content-addressed
            # key cannot both believe they created it.
            blob.upload_blob(data, length=length, overwrite=False)
        except ResourceExistsError:
            return False
        return True

    # -- reads ----------------------------------------------------------------

    def read(self, key: str) -> bytes:
        return bytes(self._container.get_blob_client(key).download_blob().readall())

    def exists(self, key: str) -> bool:
        return bool(self._container.get_blob_client(key).exists())

    def copy_to(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Download to a sibling temporary file first so a failed or cancelled
        # transfer can never leave a truncated file at the destination path,
        # which downstream FFmpeg probing would read as a corrupt asset.
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.download")
        try:
            downloader = self._container.get_blob_client(key).download_blob()
            with temporary.open("wb") as stream:
                downloader.readinto(stream)
            shutil.move(str(temporary), str(destination))
        finally:
            temporary.unlink(missing_ok=True)

    # -- signed reads ---------------------------------------------------------

    def _user_delegation_key(self, now: dt.datetime) -> Any:
        if (
            self._delegation_key is None
            or self._delegation_expiry is None
            or now + _DELEGATION_REFRESH >= self._delegation_expiry
        ):
            expiry = now + _DELEGATION_LIFETIME
            self._delegation_key = self._service.get_user_delegation_key(
                key_start_time=now - dt.timedelta(minutes=5),
                key_expiry_time=expiry,
            )
            self._delegation_expiry = expiry
        return self._delegation_key

    def signed_read_url(self, key: str, expires_in_seconds: int = 900) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        if expires_in_seconds <= 0:
            raise ValueError("expiry must be positive")
        if expires_in_seconds > self._signed_url_max_seconds:
            raise ValueError("expiry exceeds the configured signed URL maximum")
        if not self.exists(key):
            raise FileNotFoundError(key)
        now = self._clock()
        token = generate_blob_sas(
            account_name=self._service.account_name or "",
            container_name=self._container_name,
            blob_name=key,
            user_delegation_key=self._user_delegation_key(now),
            permission=BlobSasPermissions(read=True),
            expiry=now + dt.timedelta(seconds=expires_in_seconds),
            start=now - dt.timedelta(minutes=5),
        )
        return f"{self._container.url}/{quote(key)}?{token}"
