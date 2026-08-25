from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

BLOCK_SIZE = 64 * 1024
MASK_64 = (1 << 64) - 1


def opensubtitles_movie_hash(path: Path) -> str:
    """Return the OpenSubtitles 64-bit hash without loading the video into memory."""
    size = path.stat().st_size
    value = size
    with path.open("rb") as stream:
        value = _add_block(stream, value)
        stream.seek(max(0, size - BLOCK_SIZE))
        value = _add_block(stream, value)
    return f"{value:016x}"


def _add_block(stream: BinaryIO, value: int) -> int:
    read = stream.read
    for _ in range(BLOCK_SIZE // 8):
        chunk = read(8)
        if len(chunk) != 8:
            break
        value = (value + struct.unpack("<Q", chunk)[0]) & MASK_64
    return value
