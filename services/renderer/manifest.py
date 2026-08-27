"""Canonical immutable manifest serialization and identity."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(value: BaseModel | Any) -> bytes:
    payload = (
        value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def render_identity(material: BaseModel | Any) -> str:
    return hashlib.sha256(canonical_json(material)).hexdigest()


def bound_manifest_identity(manifest: BaseModel) -> str:
    """Bind every material immutable field while excluding envelope metadata."""
    payload = manifest.model_dump(
        mode="json",
        exclude={"render_identity", "manifest_id", "idempotency_key", "created_at"},
        exclude_none=True,
    )
    payload.pop("provenance", None)
    return render_identity(payload)


def reproducibility_hash(payload: dict[str, Any]) -> str:
    excluded = {"created_at", "hostname", "temporary_path", "ffmpeg_banner"}
    stable = {key: value for key, value in payload.items() if key not in excluded}
    return render_identity(stable)
