from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from vidgen.db.models import Asset
from vidgen.db.repositories import AssetRepository
from vidgen.storage.blob import BlobStore
from vidgen.storage.content_address import content_key, sha256_bytes, sha256_file


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
        return self._store(
            digest=digest,
            byte_size=len(content),
            put=lambda key: self.blob_store.put_if_absent(key, content),
            kind=kind,
            media_type=media_type,
            project_id=project_id,
            parent_asset_ids=parent_asset_ids,
            provider=provider,
            provider_request_id=provider_request_id,
            idempotency_key=idempotency_key,
            generation_parameters=generation_parameters,
            metadata=metadata,
        )

    def store_file(
        self,
        *,
        path: Path,
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
        digest, byte_size = sha256_file(path)
        return self._store(
            digest=digest,
            byte_size=byte_size,
            put=lambda key: self.blob_store.put_file_if_absent(key, path),
            kind=kind,
            media_type=media_type,
            project_id=project_id,
            parent_asset_ids=parent_asset_ids,
            provider=provider,
            provider_request_id=provider_request_id,
            idempotency_key=idempotency_key,
            generation_parameters=generation_parameters,
            metadata=metadata,
        )

    def _store(
        self,
        *,
        digest: str,
        byte_size: int,
        put: Callable[[str], bool],
        kind: str,
        media_type: str,
        project_id: UUID | None,
        parent_asset_ids: tuple[UUID, ...],
        provider: str | None,
        provider_request_id: str | None,
        idempotency_key: str | None,
        generation_parameters: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
    ) -> StoredAsset:
        if idempotency_key is not None:
            previous = self.assets.get_by_idempotency(project_id, idempotency_key)
            if previous is not None:
                if previous.sha256 != digest:
                    raise ValueError("idempotency key already used for different content")
                if not self.blob_store.exists(previous.storage_key):
                    put(previous.storage_key)
                return self._result(previous, deduplicated=True)

        blob_asset = self.assets.get_by_hash(digest)
        key = blob_asset.storage_key if blob_asset is not None else content_key(digest)
        blob_reused = not put(key)
        parents = [self.session.get(Asset, parent_id) for parent_id in parent_asset_ids]
        if any(parent is None for parent in parents):
            raise ValueError("all parent assets must exist")
        asset = Asset(
            project_id=project_id,
            kind=kind,
            sha256=digest,
            byte_size=byte_size,
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
        return self._result(asset, deduplicated=blob_reused)

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
