"""Run or resume T13 storyboard generation and deterministic timing."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from services.storyboard.commands import (
    StoryboardCommandOptions,
    generate_storyboard,
    resolve_capability,
)
from services.storyboard.retimer import format_us
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--model", default=os.getenv("VIDGEN_STORYBOARD_MODEL", "gpt-5.6"))
    parser.add_argument(
        "--capability-profile",
        default=os.getenv("VIDGEN_VISUAL_CAPABILITY_PROFILE"),
        help="configured visual-provider capability profile ID",
    )
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()

    options = StoryboardCommandOptions(
        provider=args.provider,
        model=args.model,
        capability_profile_id=args.capability_profile,
        idempotency_key=args.idempotency_key,
        openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
    )
    capability = resolve_capability(options)
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        result = await generate_storyboard(
            session, store, project_id=args.project_id, options=options
        )
        print(f"storyboard_run_id={result.storyboard_run_id}")
        print(f"storyboard_id={result.storyboard_id}")
        print(f"segments={result.segment_count} shots={result.shot_count}")
        print(
            "total_measured_duration_seconds="
            f"{format_us(result.total_duration_us)} ({result.total_duration_us} us)"
        )
        print(f"repair_attempts={result.repair_attempt_count}")
        print(f"storyboard_asset_id={result.storyboard_asset_id}")
        print(f"timing_manifest_asset_id={result.timing_manifest_asset_id}")
        print(f"validation_report_asset_id={result.validation_report_asset_id}")
        print(
            f"capability_profile={capability.capability_profile_id} "
            f"hash={capability.capability_hash}"
        )
        print(f"provider={result.provider} model={result.model}")
        print(
            f"cost estimated={result.estimated_cost} actual={result.actual_cost} {result.currency}"
        )
        print(f"selected={result.selected}")
        print(f"status={result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
