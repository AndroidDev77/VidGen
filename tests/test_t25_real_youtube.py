"""Opt-in, manually triggered integration test against a real YouTube channel.

**This test is skipped unless it is explicitly enabled.** It is never part of
ordinary pull-request CI, because it uploads to a real channel, consumes a real
quota allowance and leaves a real video behind.

To run it:

    export VIDGEN_YOUTUBE_REAL_TEST=1
    export VIDGEN_YOUTUBE_OAUTH_CLIENT_ID=...
    export VIDGEN_YOUTUBE_OAUTH_CLIENT_SECRET=...
    export VIDGEN_YOUTUBE_REAL_TEST_REFRESH_TOKEN=...      # from connect_youtube.py
    export VIDGEN_YOUTUBE_REAL_TEST_CHANNEL_ID=UC...       # the expected channel
    uv run pytest tests/test_t25_real_youtube.py -q -s

What it does, and what it deliberately does not:

* uploads a tiny synthetic MP4 as **private**, with subscriber notifications
  off and the synthetic-media disclosure set;
* uploads a synthetic SRT caption track;
* uploads a synthetic thumbnail when the channel is permitted to set one;
* waits for processing and asserts the privacy state YouTube actually reports;
* prints the created video ID so it can be deleted by hand;
* **never** makes the video unlisted or public, and never deletes anything.

Clean up afterwards in YouTube Studio using the printed video ID.
"""

from __future__ import annotations

import asyncio
import os
from io import BytesIO

import pytest

from services.publisher import youtube as capabilities
from services.publisher.contracts import VideoMetadata, YouTubeProviderError
from services.publisher.credentials import SecretValue
from services.publisher.processing import normalize
from services.publisher.resumable import ResumableUploader
from services.publisher.youtube_adapter import YouTubeDataApiProvider
from vidgen.contracts.publication import ProcessingState

ENABLED = os.getenv("VIDGEN_YOUTUBE_REAL_TEST", "").strip() not in {"", "0", "false"}

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason=(
        "the real YouTube integration test is opt-in: set VIDGEN_YOUTUBE_REAL_TEST=1 and the "
        "credentials documented in this module's docstring. It never runs in pull-request CI."
    ),
)

#: A few hundred kilobytes: enough to exercise more than one resumable chunk,
#: small enough to be a negligible upload.
SYNTHETIC_BYTES = 3 * capabilities.RESUMABLE_CHUNK_GRANULARITY


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _synthetic_mp4() -> bytes:
    """A tiny, real MP4 produced by FFmpeg. Never a fabricated container."""
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "synthetic.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=navy:s=640x360:d=3:r=24",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_bytes()


def _synthetic_srt() -> bytes:
    return (
        b"1\n00:00:00,000 --> 00:00:02,000\nVidGen integration test caption.\n\n"
        b"2\n00:00:02,000 --> 00:00:03,000\nSecond cue.\n"
    )


def _synthetic_thumbnail() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (1280, 720), (20, 40, 80)).save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


class _BytesSource:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    @property
    def byte_size(self) -> int:
        return len(self._payload)

    @property
    def media_type(self) -> str:
        return capabilities.CANONICAL_VIDEO_MEDIA_TYPE

    def read_range(self, start: int, length: int) -> bytes:
        return self._payload[start : start + length]


def test_a_private_upload_against_a_real_test_channel() -> None:
    client_id = _require("VIDGEN_YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = _require("VIDGEN_YOUTUBE_OAUTH_CLIENT_SECRET")
    refresh_token = _require("VIDGEN_YOUTUBE_REAL_TEST_REFRESH_TOKEN")
    expected_channel = _require("VIDGEN_YOUTUBE_REAL_TEST_CHANNEL_ID")

    provider = YouTubeDataApiProvider(client_id=client_id, client_secret=client_secret)

    async def run() -> None:
        tokens = await provider.refresh_access_token(refresh_token=SecretValue(refresh_token))
        access = tokens.access_token

        channel = await provider.fetch_channel(access_token=access)
        assert channel.channel_id == expected_channel, (
            "refusing to upload: the credential authorizes a different channel"
        )

        payload = _synthetic_mp4()
        metadata = VideoMetadata(
            title="VidGen T25 integration test (private)",
            description=(
                "Automated VidGen publication test. Private, never published. "
                "Contains synthetic media."
            ),
            tags=("vidgen", "integration test"),
            category_id=capabilities.DEFAULT_CATEGORY_ID,
            default_language="en",
            # Private, always. There is no branch in this test that makes it
            # anything else.
            privacy_status=capabilities.PrivacyStatus.PRIVATE.value,
            made_for_kids=False,
            contains_synthetic_media=True,
            embeddable=True,
            # Subscriber notifications are off, explicitly.
            notify_subscribers=False,
            publish_at=None,
        )
        session = await provider.initialize_resumable_upload(
            access_token=access,
            metadata=metadata,
            total_bytes=len(payload),
            media_type=capabilities.CANONICAL_VIDEO_MEDIA_TYPE,
        )
        uploader = ResumableUploader(provider, chunk_bytes=capabilities.MIN_CHUNK_BYTES)
        outcome = await uploader.drive(
            access_token=access,
            upload_uri=session.upload_uri,
            source=_BytesSource(payload),
            total_bytes=len(payload),
            start_offset=0,
        )
        assert outcome.completed and outcome.video_id, outcome
        video_id = outcome.video_id
        # Printed immediately, before anything else can fail: this is how the
        # video gets cleaned up by hand.
        print(f"\nCREATED YOUTUBE VIDEO (delete manually): {video_id}")
        print(f"  {capabilities.watch_url(video_id)}")

        for _ in range(60):
            snapshot = await provider.fetch_processing_status(
                access_token=access, video_id=video_id
            )
            state = normalize(snapshot)
            if state is not ProcessingState.PROCESSING:
                break
            await asyncio.sleep(10)
        assert state in {ProcessingState.SUCCEEDED, ProcessingState.PROCESSING}, snapshot

        caption = await provider.insert_caption(
            access_token=access,
            video_id=video_id,
            language="en",
            name="VidGen integration test",
            content=_synthetic_srt(),
            media_type=capabilities.CANONICAL_CAPTION_MEDIA_TYPE,
        )
        assert caption.caption_id
        print(f"  caption track: {caption.caption_id}")

        try:
            thumbnail = await provider.set_thumbnail(
                access_token=access,
                video_id=video_id,
                content=_synthetic_thumbnail(),
                media_type="image/jpeg",
            )
            print(f"  thumbnail: {thumbnail.url or 'set'}")
        except YouTubeProviderError as error:
            # A channel without custom-thumbnail permission is an expected
            # outcome, not a test failure.
            print(f"  thumbnail skipped: {error.code.value}")

        final = await provider.fetch_video(access_token=access, video_id=video_id)
        # The privacy YouTube reports, not the one that was requested.
        assert final.privacy_status == capabilities.PrivacyStatus.PRIVATE.value, final
        print(f"  actual privacy: {final.privacy_status}")

    try:
        asyncio.run(run())
    finally:
        asyncio.run(provider.aclose())
