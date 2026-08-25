from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from vidgen.db.models import Asset
from vidgen.db.repositories import AssetRepository
from vidgen.storage.blob import BlobStore
from vidgen.storage.content_address import content_key, sha256_bytes


@dataclass(frozen=True, slots=True)
class StoredAsset:
    id: UUID
    sha256: str
    storage_key: str
    byte_size: int
    media_type: str
    deduplicated: bool
    parent_ids: tuple[UUID, ...] = field(default_factory=tuple)


class AssetService:
    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self.session = session
        self.blob_store = blob_store
        self.assets = AssetRepository(session)

    def store(
        self,
        *,
        content: bytes,
        kind: str,
        media_type: str,
        project_id: UUID | None = None,
        parent_asset_ids: tuple[UUID, ...] = (),
        provider: str | None = None,
        provider_request_id: str | None = None,
        idempotency_key: str | None = None,
        generation_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredAsset:
        digest = sha256_bytes(content)
        existing = self.assets.get_by_hash(digest)
        if existing is not None:
            if not self.blob_store.exists(existing.storage_key):
                self.blob_store.put_if_absent(existing.storage_key, content)
            return self._result(existing, deduplicated=True)

        key = content_key(digest)
        self.blob_store.put_if_absent(key, content)
        parents = [self.session.get(Asset, parent_id) for parent_id in parent_asset_ids]
        if any(parent is None for parent in parents):
            raise ValueError("all parent assets must exist")
        asset = Asset(
            project_id=project_id,
            kind=kind,
            sha256=digest,
            byte_size=len(content),
            media_type=media_type,
            storage_key=key,
            provider=provider,
            provider_request_id=provider_request_id,
            idempotency_key=idempotency_key,
            generation_parameters=generation_parameters or {},
            extra_metadata=metadata or {},
            parents=[parent for parent in parents if parent is not None],
        )
        self.assets.add(asset)
        return self._result(asset, deduplicated=False)

    @staticmethod
    def _result(asset: Asset, *, deduplicated: bool) -> StoredAsset:
        return StoredAsset(
            id=asset.id,
            sha256=asset.sha256,
            storage_key=asset.storage_key,
            byte_size=asset.byte_size,
            media_type=asset.media_type,
            deduplicated=deduplicated,
            parent_ids=tuple(parent.id for parent in asset.parents),
        )
