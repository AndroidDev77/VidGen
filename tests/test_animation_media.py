from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from services.animation.downloader import download_video
from services.animation.trim import trim_video
from services.animation.validation import validate_video


def video(path: Path, *, width: int = 320, height: int = 180, duration: float = 2) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s={width}x{height}:r=24:d={duration}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    return path


def test_probe_validation_and_deterministic_trim(tmp_path: Path) -> None:
    source = video(tmp_path / "source.mp4")
    report = validate_video(
        source,
        expected_width=320,
        expected_height=180,
        requested_duration=2,
        minimum_usable_duration=1.5,
    )
    assert report.valid
    first = trim_video(
        source, trim_in_seconds=0.25, trim_out_seconds=0.25, usable_duration_seconds=1.5
    )
    second = trim_video(
        source, trim_in_seconds=0.25, trim_out_seconds=0.25, usable_duration_seconds=1.5
    )
    try:
        assert first.output_sha256 == second.output_sha256
        assert first.probe.duration_seconds == pytest.approx(1.5, abs=1 / 24)
    finally:
        first.path.unlink(missing_ok=True)
        second.path.unlink(missing_ok=True)


def test_corrupt_and_wrong_dimensions_are_rejected(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.mp4"
    corrupt.write_bytes(b"truncated")
    assert not validate_video(
        corrupt,
        expected_width=320,
        expected_height=180,
        requested_duration=2,
        minimum_usable_duration=1,
    ).valid
    wrong = video(tmp_path / "wrong.mp4", width=336)
    report = validate_video(
        wrong,
        expected_width=320,
        expected_height=180,
        requested_duration=2,
        minimum_usable_duration=1,
    )
    assert not report.valid
    assert any(item.code == "incorrect_dimensions" for item in report.diagnostics)


def test_local_streaming_download_enforces_size(tmp_path: Path) -> None:
    source = video(tmp_path / "source.mp4")
    downloaded = asyncio.run(download_video(source.as_uri(), max_bytes=source.stat().st_size + 1))
    try:
        assert downloaded.sha256
        assert downloaded.byte_size == source.stat().st_size
    finally:
        downloaded.path.unlink(missing_ok=True)
    with pytest.raises(ValueError, match="maximum byte size"):
        asyncio.run(download_video(source.as_uri(), max_bytes=16))
