"""Content-addressed asset storage."""

from vidgen.storage.asset_service import AssetService, StoredAsset
from vidgen.storage.blob import BlobStore, FilesystemBlobStore
from vidgen.storage.factory import build_blob_store

__all__ = [
    "AssetService",
    "BlobStore",
    "FilesystemBlobStore",
    "StoredAsset",
    "build_blob_store",
]
