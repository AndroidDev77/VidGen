"""Content-addressed asset storage."""

from vidgen.storage.asset_service import AssetService, StoredAsset
from vidgen.storage.blob import FilesystemBlobStore

__all__ = ["AssetService", "FilesystemBlobStore", "StoredAsset"]
