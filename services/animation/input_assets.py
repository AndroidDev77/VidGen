"""Resolve verified T14 assets to bounded, ephemeral Runway inputs."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from services.animation.providers import VideoCapability
from vidgen.db.models import Asset
from vidgen.storage.blob import BlobStore


@dataclass(frozen=True, slots=True)
class ResolvedInputAsset:
    asset_id: str
    sha256: str
    width: int
    height: int
    media_type: str
    data_uri: str


def resolve_input_asset(
    blob_store: BlobStore,
    asset: Asset,
    capability: VideoCapability,
    *,
    expected_width: int,
    expected_height: int,
) -> ResolvedInputAsset:
    if asset.media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("invalid_input_asset: unsupported image Content-Type")
    content = blob_store.read(asset.storage_key)
    digest = hashlib.sha256(content).hexdigest()
    if digest != asset.sha256:
        raise ValueError("invalid_input_asset: persisted keyframe hash mismatch")
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
    except Exception as error:
        raise ValueError("invalid_input_asset: keyframe is not decodable") from error
    if width * expected_height != height * expected_width:
        raise ValueError(
            "keyframe_aspect_ratio_mismatch: deterministic fit is required before submission"
        )
    encoded = base64.b64encode(content).decode("ascii")
    data_uri = f"data:{asset.media_type};base64,{encoded}"
    if len(data_uri.encode("ascii")) > capability.max_input_bytes:
        raise ValueError("invalid_input_asset: encoded data URI exceeds provider size limit")
    return ResolvedInputAsset(
        asset_id=str(asset.id),
        sha256=digest,
        width=width,
        height=height,
        media_type=asset.media_type,
        data_uri=data_uri,
    )
