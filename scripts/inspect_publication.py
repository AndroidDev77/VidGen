"""Inspect a project's publications without contacting YouTube.

    uv run python scripts/inspect_publication.py PROJECT_UUID

Read-only: it makes no provider request, spends no quota, and prints no
credential, no authorization code and no resumable session URI. The upload
session is shown only by the first bytes of its URI *hash*, which is evidence
that two checkpoints refer to the same session and nothing more.
"""

from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

from services.publisher.commands import keyring_from_settings
from services.publisher.projections import (
    attempt_projections,
    latest_session,
    result_projection,
)
from vidgen.db.publication_repository import PublicationRepository
from vidgen.db.session import build_engine, session_factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--publication-id", type=UUID)
    parser.add_argument("--owner", default=os.getenv("VIDGEN_LOCAL_OWNER", "local-user"))
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    keyring = keyring_from_settings(allow_development_key=True)
    with session_factory(build_engine())() as session:
        repository = PublicationRepository(session, keyring)
        runs = [
            run
            for run in repository.runs_for_project(arguments.project_id)
            if run.owner_subject == arguments.owner
            and (arguments.publication_id is None or run.id == arguments.publication_id)
        ]
        if not runs:
            print("no publications for this project")
            return 1
        payloads: list[dict[str, object]] = []
        for run in runs:
            result = result_projection(session, run)
            upload = latest_session(session, run.id)
            attempts = attempt_projections(session, run)
            payload: dict[str, object] = {
                "publication_run_id": str(result.publication_run_id),
                "status": result.status.value,
                "phase": result.phase.value,
                "channel_id": result.channel_id,
                "render_asset_id": str(result.final_render_asset_id),
                "publication_identity": result.publication_identity,
                "metadata_version": result.metadata_version,
                "video_id": result.video_id,
                "video_url": result.video_url,
                "total_bytes": result.total_bytes,
                "confirmed_offset": result.confirmed_offset,
                "session_uri_hash_prefix": (upload.session_uri_hash[:16] if upload else None),
                "session_status": upload.status if upload else None,
                "processing_state": (
                    result.processing_state.value if result.processing_state else None
                ),
                "caption_status": result.caption_status.value if result.caption_status else None,
                "caption_track_id": result.caption_track_id or None,
                "thumbnail_status": (
                    result.thumbnail_status.value if result.thumbnail_status else None
                ),
                "requested_privacy": result.requested_privacy.value,
                "actual_privacy": result.actual_privacy.value if result.actual_privacy else None,
                "contains_synthetic_media": result.contains_synthetic_media,
                "quota_units": result.quota_units,
                "attempts": [
                    {
                        "operation": attempt.operation,
                        "status": attempt.status,
                        "quota_units": attempt.quota_units,
                        "latency_ms": attempt.latency_ms,
                        "failure": attempt.failure.code.value if attempt.failure else None,
                    }
                    for attempt in attempts
                ],
                "failure": result.failure.code.value if result.failure else None,
            }
            payloads.append(payload)
            if not arguments.json:
                _render(payload)
    if arguments.json:
        print(json.dumps(payloads, indent=2, sort_keys=True))
    return 0


def _render(payload: dict[str, object]) -> None:
    print(f"publication_run_id={payload['publication_run_id']}")
    print(f"  status={payload['status']} phase={payload['phase']}")
    print(f"  channel_id={payload['channel_id']}")
    print(f"  render_asset_id={payload['render_asset_id']}")
    print(f"  identity={str(payload['publication_identity'])[:16]}...")
    print(f"  video_id={payload['video_id']} url={payload['video_url']}")
    print(
        f"  upload={payload['confirmed_offset']}/{payload['total_bytes']} bytes "
        f"session={payload['session_uri_hash_prefix']} ({payload['session_status']})"
    )
    print(f"  processing_state={payload['processing_state']}")
    print(f"  caption={payload['caption_status']} id={payload['caption_track_id']}")
    print(f"  thumbnail={payload['thumbnail_status']}")
    print(
        f"  requested_privacy={payload['requested_privacy']} "
        f"actual_privacy={payload['actual_privacy']}"
    )
    print(f"  synthetic_media_disclosed={payload['contains_synthetic_media']}")
    print(f"  quota_units={payload['quota_units']} (YouTube API units, not a charge)")
    attempts = payload["attempts"]
    if isinstance(attempts, list):
        for attempt in attempts:
            print(f"    attempt {attempt}")
    if payload["failure"]:
        print(f"  failure={payload['failure']}")


if __name__ == "__main__":
    raise SystemExit(main())
