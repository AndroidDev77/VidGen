"""Generate or resume T14 keyframes from the selected T13 storyboard."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from services.image_generation.commands import ImageGenerationCommandOptions, generate_keyframes
from vidgen.contracts.image_generation import KeyframeRole
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--shot-id", type=UUID)
    parser.add_argument("--role", choices=[item.value for item in KeyframeRole])
    parser.add_argument("--width", type=int, default=1536)
    parser.add_argument("--height", type=int, default=864)
    parser.add_argument("--quality", choices=("low", "medium", "high"), default="medium")
    args = parser.parse_args()
    options = ImageGenerationCommandOptions(
        provider=args.provider,
        model=os.getenv("VIDGEN_IMAGE_MODEL", "gpt-image-2-2026-04-21"),
        width=args.width,
        height=args.height,
        quality=args.quality,
        idempotency_key=args.idempotency_key,
        openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        result = await generate_keyframes(
            session,
            store,
            project_id=args.project_id,
            options=options,
            shot_id=args.shot_id,
            role=KeyframeRole(args.role) if args.role else None,
        )
        print(f"image_generation_run_id={result.run_id}")
        print(f"storyboard_id={result.storyboard_id} version={result.storyboard_version}")
        print(
            f"requested={result.requested_count} completed={result.completed_count} "
            f"reused={result.reused_count} failed={result.failed_count}"
        )
        print(f"provider={args.provider} model={options.model}")
        for item in result.items:
            candidate = item.candidate
            print(
                f"shot_id={item.shot_id} role={item.keyframe_role.value} "
                f"prompt_hash={item.prompt_hash} "
                f"generated_image_id={candidate.generated_image_id if candidate else None} "
                f"asset_id={candidate.asset_id if candidate else None} "
                f"requested_dimensions={options.width}x{options.height} "
                f"actual_dimensions={candidate.validation.width if candidate else None}x"
                f"{candidate.validation.height if candidate else None}"
            )
        print("total_cost=see scripts/cost_report.py")
        print(f"status={result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
