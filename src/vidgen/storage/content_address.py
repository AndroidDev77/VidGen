from __future__ import annotations

import hashlib


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def content_key(sha256: str) -> str:
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be a lowercase hexadecimal digest")
    return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"
