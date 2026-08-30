"""Publish one project's approved, current, T22-passing render to YouTube.

Fake mode is fully deterministic and needs no Google project, no credential and
no network:

    uv run python scripts/publish_youtube.py PROJECT_UUID --provider fake

Production uses a connected channel and the real Data API:

    uv run python scripts/publish_youtube.py PROJECT_UUID \
        --provider youtube \
        --connection-id CONNECTION_UUID \
        --privacy private

Every upload starts private and never notifies subscribers. Making a video
unlisted or public is a separate, explicit action:

    uv run python scripts/publish_youtube.py PROJECT_UUID --visibility unlisted \
        --publication-id PUBLICATION_UUID

Nothing printed here is a credential: no token, no authorization code and no
resumable session URI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from services.publisher.commands import (
    PublisherCommandOptions,
    apply_visibility,
    publish_project,
)
from services.publisher.eligibility import PublicationEligibilityError
from services.publisher.oauth import OAuthFlowError
from services.publisher.pipeline import PublicationError
from services.publisher.providers import FAKE_PROVIDER, YOUTUBE_PROVIDER
from services.publisher.youtube import DEFAULT_CHUNK_BYTES, normalize_chunk_bytes
from vidgen.contracts.publication import PrivacyState, PublicationResult
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


def _compact(result: PublicationResult) -> dict[str, object]:
    return {
        "publication_run_id": str(result.publication_run_id),
        "project_id": str(result.project_id),
        "connection_id": str(result.connection_id),
        "channel_id": result.channel_id,
        "render_asset_id": str(result.final_render_asset_id),
        "final_editorial_run_id": str(result.final_editorial_run_id),
        "publication_identity": result.publication_identity,
        "metadata_version": result.metadata_version,
        "total_bytes": result.total_bytes,
        "confirmed_offset": result.confirmed_offset,
        "video_id": result.video_id,
        "video_url": result.video_url,
        "processing_state": result.processing_state.value if result.processing_state else None,
        "caption_status": result.caption_status.value if result.caption_status else None,
        "caption_track_id": result.caption_track_id or None,
        "thumbnail_status": result.thumbnail_status.value if result.thumbnail_status else None,
        "requested_privacy": result.requested_privacy.value,
        "actual_privacy": result.actual_privacy.value if result.actual_privacy else None,
        "scheduled_publish_at": (
            result.scheduled_publish_at.isoformat() if result.scheduled_publish_at else None
        ),
        "contains_synthetic_media": result.contains_synthetic_media,
        "made_for_kids": result.made_for_kids,
        "notify_subscribers": result.notify_subscribers,
        "quota_units": result.quota_units,
        "capability_profile": result.capability_profile_version,
        "status": result.status.value,
        "phase": result.phase.value,
        "failure": result.failure.code.value if result.failure else None,
    }


def render(result: PublicationResult) -> None:
    compact = _compact(result)
    print(f"publication_run_id={compact['publication_run_id']}")
    print(f"  channel_id={compact['channel_id']}")
    print(f"  render_asset_id={compact['render_asset_id']}")
    print(f"  total_bytes={compact['total_bytes']} confirmed_offset={compact['confirmed_offset']}")
    print(f"  video_id={compact['video_id']}")
    print(f"  video_url={compact['video_url'] or 'not yet created'}")
    print(f"  processing_state={compact['processing_state']}")
    print(f"  caption_status={compact['caption_status']} caption_id={compact['caption_track_id']}")
    print(f"  thumbnail_status={compact['thumbnail_status']}")
    print(
        f"  requested_privacy={compact['requested_privacy']} "
        f"actual_privacy={compact['actual_privacy']}"
    )
    print(f"  synthetic_media_disclosed={compact['contains_synthetic_media']}")
    # A rate-limit quantity, not money. Recorded on the T23 attempt with zero cost.
    print(f"  quota_units={compact['quota_units']} (YouTube API units, not a charge)")
    print(f"  status={compact['status']} phase={compact['phase']}")
    if compact["failure"]:
        print(f"  failure={compact['failure']}: {result.failure.summary if result.failure else ''}")
        if result.failure and result.failure.remediation:
            print(f"  next_step={result.failure.remediation}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument(
        "--provider", choices=(FAKE_PROVIDER, YOUTUBE_PROVIDER), default=FAKE_PROVIDER
    )
    parser.add_argument("--connection-id", type=UUID)
    parser.add_argument("--thumbnail-asset-id", type=UUID)
    parser.add_argument("--publication-id", type=UUID, help="required with --visibility")
    parser.add_argument("--owner", default=os.getenv("VIDGEN_LOCAL_OWNER", "local-user"))
    parser.add_argument("--idempotency-key")
    parser.add_argument(
        "--privacy",
        choices=tuple(state.value for state in PrivacyState),
        default=PrivacyState.PRIVATE.value,
        help="the eventual privacy the draft requests; the upload is always private",
    )
    parser.add_argument(
        "--visibility",
        choices=tuple(state.value for state in PrivacyState),
        help="apply an explicit visibility change to an already-uploaded video",
    )
    parser.add_argument("--notify-subscribers", action="store_true")
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    arguments = parser.parse_args()

    options = PublisherCommandOptions(
        provider=arguments.provider,
        connection_id=arguments.connection_id,
        thumbnail_asset_id=arguments.thumbnail_asset_id,
        idempotency_key=arguments.idempotency_key,
        chunk_bytes=normalize_chunk_bytes(arguments.chunk_bytes),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".local-data/blobs")),
        os.getenv("VIDGEN_SIGNING_SECRET", "local-development-only-change-me").encode(),
    )
    with session_factory(build_engine())() as session:
        try:
            if arguments.visibility:
                if arguments.publication_id is None:
                    parser.error("--visibility requires --publication-id")
                result = await apply_visibility(
                    session,
                    store,
                    publication_run_id=arguments.publication_id,
                    project_id=arguments.project_id,
                    owner_subject=arguments.owner,
                    privacy=PrivacyState(arguments.visibility),
                    notify_subscribers=arguments.notify_subscribers,
                    options=options,
                )
            else:
                result = await publish_project(
                    session,
                    store,
                    project_id=arguments.project_id,
                    owner_subject=arguments.owner,
                    options=options,
                )
        except PublicationEligibilityError as error:
            for failure in error.gate.failures:
                print(f"cannot publish: [{failure.code.value}] {failure.summary}")
                if failure.remediation:
                    print(f"  next_step={failure.remediation}")
            return 2
        except (PublicationError, OAuthFlowError) as error:
            print(f"publication failed: {error}")
            return 1
    if arguments.json:
        print(json.dumps(_compact(result), indent=2, sort_keys=True))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
