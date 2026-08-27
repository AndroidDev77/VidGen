"""Streaming asset staging with content and size validation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from services.renderer.render import contained


def stage_chunks(
    chunks: Iterable[bytes],
    destination: Path,
    *,
    root: Path,
    expected_sha256: str,
    max_bytes: int = 2_000_000_000,
) -> int:
    destination = contained(root, destination)
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as stream:
        for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("staged asset exceeds configured size cap")
            digest.update(chunk)
            stream.write(chunk)
    if digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("staged asset hash mismatch")
    return total
