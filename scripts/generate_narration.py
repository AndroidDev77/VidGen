"""Run or resume segmented narration generation."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from services.narration.commands import NarrationCommandOptions, generate_narration
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai", "elevenlabs"), default="fake")
    parser.add_argument("--voice-profile-id", type=UUID, required=True)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        result = await generate_narration(
            session,
            store,
            project_id=args.project_id,
            options=NarrationCommandOptions(
                provider=args.provider,
                voice_profile_id=args.voice_profile_id,
                idempotency_key=args.idempotency_key,
                openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
                elevenlabs_api_key=os.getenv("VIDGEN_ELEVENLABS_API_KEY"),
            ),
        )
        print(f"narration_run_id={result.narration_run_id}")
        print(f"status={result.status}")
        print(f"segments={len(result.segments)} attempts={sum(1 for _ in result.segments)}")
        for segment in result.segments:
            print(
                f"segment={segment.sequence} duration={segment.duration_seconds:.6f} "
                f"coverage={segment.alignment.coverage:.6f}"
            )
        print(f"preview_manifest_asset_id={result.preview_manifest_asset_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
