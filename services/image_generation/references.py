"""Project-owned, hash-verified image reference resolution."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from uuid import UUID

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from services.image_generation.providers import DEFAULT_LIMITS
from vidgen.contracts.image_generation import ImageReferenceBinding
from vidgen.db.models import Asset
from vidgen.storage.blob import BlobStore


class ReferenceValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedReferences:
    bindings: tuple[ImageReferenceBinding, ...]
    contents: tuple[bytes, ...]


def resolve_references(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    bindings: list[ImageReferenceBinding],
    max_count: int = DEFAULT_LIMITS.max_references,
) -> ResolvedReferences:
    ordered = sorted(bindings, key=lambda item: (not item.required, item.order, str(item.asset_id)))
    required = [item for item in ordered if item.required]
    if len(required) > max_count:
        raise ReferenceValidationError("required references exceed provider count limit")
    selected = (
        required + [item for item in ordered if not item.required][: max_count - len(required)]
    )
    selected.sort(key=lambda item: (item.order, item.semantic_role, str(item.asset_id)))
    contents: list[bytes] = []
    for binding in selected:
        asset = session.get(Asset, binding.asset_id)
        if asset is None:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} does not exist")
        if asset.project_id != project_id:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} is cross-project")
        if asset.media_type != binding.media_type:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} MIME mismatch")
        data = blob_store.read(asset.storage_key)
        if len(data) > DEFAULT_LIMITS.max_reference_bytes:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} is too large")
        if hashlib.sha256(data).hexdigest() != binding.sha256 or asset.sha256 != binding.sha256:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} hash mismatch")
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise ReferenceValidationError(
                f"reference asset {binding.asset_id} is corrupt"
            ) from exc
        detected = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
            image.format or ""
        )
        if detected != binding.media_type:
            raise ReferenceValidationError(f"reference asset {binding.asset_id} format mismatch")
        contents.append(data)
    return ResolvedReferences(tuple(selected), tuple(contents))
