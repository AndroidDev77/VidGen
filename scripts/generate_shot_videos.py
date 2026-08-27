"""Generate or resume T15 shot videos from authoritative T13/T14 inputs."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from services.animation.commands import AnimationCommandOptions, generate_shot_videos
from vidgen.contracts.animation import RunwayModel
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "runway"), default="fake")
    parser.add_argument("--model", choices=[item.value for item in RunwayModel])
    parser.add_argument("--idempotency-key")
    parser.add_argument("--shot-id", type=UUID)
    args = parser.parse_args()
    options = AnimationCommandOptions(
        provider=args.provider,
        model=RunwayModel(args.model) if args.model else None,
        idempotency_key=args.idempotency_key,
        runway_api_key=os.getenv("RUNWAYML_API_SECRET"),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        result = await generate_shot_videos(
            session,
            store,
            project_id=args.project_id,
            options=options,
            shot_id=args.shot_id,
        )
        print(f"animation_run_id={result.run_id}")
        print(
            f"storyboard_id={result.storyboard_id} "
            f"image_generation_run_id={result.image_generation_run_id}"
        )
        print(
            f"requested={result.requested_count} submitted={result.submitted_count} "
            f"polling={result.polling_count} completed={result.completed_count} "
            f"reused={result.reused_count} failed={result.failed_count}"
        )
        for item in result.items:
            candidate = item.candidate
            print(
                f"shot_id={item.shot_id} provider={args.provider} "
                f"model={options.model.value if options.model else 'routed'} "
                f"remote_task_id={item.remote_task_id} "
                f"original_asset_id={candidate.original_asset_id if candidate else None} "
                f"canonical_asset_id={candidate.canonical_asset_id if candidate else None}"
            )
            if candidate and candidate.validation.probe:
                print(
                    f"requested_duration=see_t13 measured_duration="
                    f"{candidate.validation.probe.duration_seconds}"
                )
        print("cost_summary=see scripts/cost_report.py")
        print(f"status={result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
