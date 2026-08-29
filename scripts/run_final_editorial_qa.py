"""Run or resume T22 final editorial QA for one project's current render.

Fake mode is fully deterministic and needs no paid credential:

    uv run python scripts/run_final_editorial_qa.py PROJECT_UUID --provider fake

The configured production provider reads its key from the environment:

    VIDGEN_OPENAI_API_KEY=... uv run python scripts/run_final_editorial_qa.py PROJECT_UUID \
        --provider openai
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from services.qa.final_commands import FinalQACommandOptions, run_final_editorial_qa
from services.qa.final_inputs import FinalQALineageError
from vidgen.contracts.final_editorial import FinalEditorialResult
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


def _compact(result: FinalEditorialResult) -> dict[str, object]:
    return {
        "final_editorial_run_id": str(result.final_editorial_run_id),
        "project_id": str(result.project_id),
        "final_render_asset_id": str(result.final_video_asset_id),
        "render_manifest_asset_id": str(result.render_manifest_asset_id),
        "final_qa_identity": result.final_qa_identity,
        "input_hash": result.input_hash,
        "configuration_hash": result.configuration_hash,
        "status": result.status.value,
        "phase": result.phase.value,
        "deterministic_checks": result.deterministic_check_count,
        "deterministic_failures": result.deterministic_failure_count,
        "audio_checks": result.audio_check_count,
        "audio_failures": result.audio_failure_count,
        "caption_checks": result.caption_check_count,
        "caption_failures": result.caption_failure_count,
        "blocking_findings": result.blocking_finding_count,
        "review_findings": result.review_finding_count,
        "warning_findings": result.warning_finding_count,
        "remediation_targets": [target.value for target in result.remediation_targets],
        "provider": result.first_pass_provider,
        "model": result.first_pass_model,
        "adjudication": (
            f"decided(confidence={result.adjudication_confidence:.2f})"
            if result.adjudicated and result.adjudication_confidence is not None
            else "performed"
            if result.adjudicated
            else "not_required"
        ),
        "cost_microusd": result.cost_microusd,
        "report_asset_id": str(result.report_asset_id) if result.report_asset_id else None,
        "decision": result.decision.value if result.decision else None,
        "reused": result.reused,
    }


def _print(result: FinalEditorialResult) -> None:
    compact = _compact(result)
    print(f"final_editorial_run_id={compact['final_editorial_run_id']}")
    print(f"  final_render_asset_id={compact['final_render_asset_id']}")
    print(
        f"  input_identity=final_qa:{result.final_qa_identity[:16]} "
        f"input_hash={result.input_hash[:16]} configuration_hash={result.configuration_hash[:16]}"
    )
    print(
        f"  deterministic_checks={compact['deterministic_checks']} "
        f"failures={compact['deterministic_failures']}"
    )
    print(
        f"  audio_checks={compact['audio_checks']} failures={compact['audio_failures']} "
        f"caption_checks={compact['caption_checks']} failures={compact['caption_failures']}"
    )
    print(
        f"  blocking={compact['blocking_findings']} review={compact['review_findings']} "
        f"warning={compact['warning_findings']}"
    )
    targets = ",".join(target.value for target in result.remediation_targets) or "none"
    print(f"  remediation_targets={targets}")
    print(f"  provider={result.first_pass_provider}/{result.first_pass_model}")
    print(f"  adjudication={compact['adjudication']}")
    print(f"  cost_microusd={result.cost_microusd}")
    print(f"  report_asset_id={compact['report_asset_id']}")
    print(f"  gate_decision={compact['decision']}")
    print(f"  status={result.status.value}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--idempotency-key", help="resume an existing run by its key")
    parser.add_argument("--no-adjudication", action="store_true")
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    arguments = parser.parse_args()
    options = FinalQACommandOptions(
        provider=arguments.provider,
        idempotency_key=arguments.idempotency_key,
        adjudicate=not arguments.no_adjudication,
        openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
        first_pass_model=os.getenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL"),
        adjudicator_model=os.getenv("VIDGEN_FINAL_QA_ADJUDICATOR_MODEL"),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        try:
            result = await run_final_editorial_qa(
                session, store, project_id=arguments.project_id, options=options
            )
        except FinalQALineageError as error:
            print(f"final QA rejected before any paid request: {error.code.value}: {error}")
            return 2
        if arguments.json:
            print(json.dumps(_compact(result), indent=2, sort_keys=True))
        else:
            _print(result)
        return 0 if result.decision is not None and result.decision.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
