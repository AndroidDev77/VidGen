"""Start or resume T11 compression and comedy script generation for a project."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.settings import get_settings
from services.script.commands import ScriptCommandOptions, generate_script
from vidgen.contracts.script import RecapScript
from vidgen.db.models import Asset
from vidgen.db.script_models import Script
from vidgen.db.session import build_engine
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--target-duration-ms", type=int)
    parser.add_argument("--target-words", type=int)
    parser.add_argument("--humor-intensity", type=float)
    parser.add_argument("--recap-mode", choices=("full_recap", "highlight_reel"))
    args = parser.parse_args()

    settings = get_settings()
    options = ScriptCommandOptions(
        provider=args.provider,
        idempotency_key=args.idempotency_key,
        target_duration_ms=args.target_duration_ms,
        target_words=args.target_words,
        humor_intensity=args.humor_intensity,
        recap_mode=args.recap_mode,
        openai_api_key=os.getenv("OPENAI_API_KEY") or settings.openai_api_key,
        compressor_model=settings.script_compressor_model,
        writer_model=settings.script_writer_model,
        editor_model=settings.script_editor_model,
    )
    if args.provider == "openai" and not options.openai_api_key:
        parser.error("VIDGEN_OPENAI_API_KEY or OPENAI_API_KEY is required for --provider openai")

    with Session(build_engine(settings.database_url), expire_on_commit=False) as session:
        blob_store = FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())
        result = await generate_script(
            session, blob_store, project_id=args.project_id, options=options
        )

        script_record = session.get(Script, result.script_id) if result.script_id else None
        mandatory_coverage = "n/a"
        if script_record is not None:
            script_contract = None
            asset = session.get(Asset, script_record.canonical_script_asset_id)
            if asset is not None:
                script_contract = RecapScript.model_validate_json(
                    blob_store.read(asset.storage_key)
                )
            if script_contract is not None:
                mandatory = [item for item in script_contract.beat_coverage if item.mandatory]
                covered = [item for item in mandatory if item.coverage == "covered"]
                mandatory_coverage = f"{len(covered)}/{len(mandatory)}" if mandatory else "0/0"

        scores = result.review_scores
        print(
            f"generation_run_id={result.generation_run_id} "
            f"compressed_plan_id={result.compressed_plot_plan_id} "
            f"script_id={result.script_id} version={result.script_version} "
            f"target_words={script_record.target_word_count if script_record else 'n/a'} "
            f"actual_words={script_record.actual_word_count if script_record else 'n/a'} "
            f"mandatory_beat_coverage={mandatory_coverage} "
            f"overall_score={scores.overall if scores else 'n/a'} "
            f"plot_fidelity_score={scores.plot_fidelity if scores else 'n/a'} "
            f"revision_count={result.revision_count} "
            f"status={result.status}"
        )


if __name__ == "__main__":
    asyncio.run(main())
