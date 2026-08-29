"""Select the configured blob backend.

Local development, the test suites and the deterministic fake-provider paths
keep the filesystem store. A deployed environment sets
``VIDGEN_BLOB_BACKEND=azure`` and reaches Blob Storage over a private endpoint
with a managed identity, so no credential ever appears in configuration.
"""

from __future__ import annotations

from typing import Protocol

from vidgen.storage.blob import BlobStore, FilesystemBlobStore

FILESYSTEM_BACKEND = "filesystem"
AZURE_BACKEND = "azure"
SUPPORTED_BACKENDS = (FILESYSTEM_BACKEND, AZURE_BACKEND)


class BlobBackendSettings(Protocol):
    """The configuration subset a blob backend needs.

    Declared structurally so the factory does not import the API settings
    module, which would make the worker depend on the API package purely to
    build a store.
    """

    @property
    def blob_backend(self) -> str: ...

    @property
    def blob_root(self) -> object: ...

    @property
    def signing_secret(self) -> str: ...

    @property
    def blob_account_url(self) -> str | None: ...

    @property
    def blob_container(self) -> str: ...


def build_blob_store(settings: BlobBackendSettings) -> BlobStore:
    backend = settings.blob_backend.strip().lower()
    if backend == FILESYSTEM_BACKEND:
        from pathlib import Path

        return FilesystemBlobStore(Path(str(settings.blob_root)), settings.signing_secret.encode())
    if backend == AZURE_BACKEND:
        from vidgen.storage.azure_blob import AzureBlobStore

        account_url = settings.blob_account_url
        if not account_url:
            raise ValueError("VIDGEN_BLOB_ACCOUNT_URL is required when the blob backend is azure")
        return AzureBlobStore(account_url=account_url, container=settings.blob_container)
    raise ValueError(f"unsupported blob backend {backend!r}; expected one of {SUPPORTED_BACKENDS}")
