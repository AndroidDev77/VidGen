"""Run or resume T20 semantic visual QA for a project or a single shot.

Fake mode is fully deterministic and needs no paid credential:

    uv run python scripts/run_visual_qa.py PROJECT_UUID --provider fake

The configured production provider reads its key from the environment:

    VIDGEN_OPENAI_API_KEY=... uv run python scripts/run_visual_qa.py PROJECT_UUID \
        --provider openai
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from services.qa.commands import VisualQACommandOptions, run_visual_qa
from vidgen.contracts.visual_qa import VisualQAResult, VisualQATargetType
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


def _targets(arguments: argparse.Namespace) -> tuple[VisualQATargetType, ...]:
    if arguments.keyframe_only:
        return (VisualQATargetType.KEYFRAME,)
    if arguments.video_only:
        return (VisualQATargetType.VIDEO,)
    return (VisualQATargetType.KEYFRAME, VisualQATargetType.VIDEO)


def _evidence_timestamps(result: VisualQAResult) -> list[int]:
    return sorted(
        {
            item.source_relative_timestamp_us
            for dimension in result.score.dimensions
            for finding in dimension.findings
            for item in finding.evidence
            if item.source_relative_timestamp_us is not None
        }
    )


def _compact(result: VisualQAResult) -> dict[str, object]:
    return {
        "qa_run_id": str(result.qa_run_id),
        "shot_id": str(result.target.storyboard_shot_id),
        "target_type": result.target.target_type.value,
        "sample_count": len(result.sampling_manifest.samples),
        "deterministic_warning_count": len(result.deterministic_report.warnings),
        "hard_failure_count": len(result.hard_failure_codes),
        "dimension_scores": {
            item.dimension.value: item.raw_score for item in result.score.dimensions
        },
        "recomputed_total": result.score.total,
        "pass_threshold": result.score.pass_threshold,
        "outcome": result.outcome.value,
        "repair_codes": [code.value for code in result.repair_codes],
        "evidence_timestamps_us": _evidence_timestamps(result),
        "first_pass_provider": result.first_pass_provider,
        "first_pass_model": result.first_pass_model,
        "adjudication": result.adjudication.resulting_outcome_hint.value
        if result.adjudication is not None
        else "not_required",
        "cost_microusd": result.cost_microusd,
    }


def _print(result: VisualQAResult) -> None:
    compact = _compact(result)
    print(f"qa_run_id={compact['qa_run_id']} shot_id={compact['shot_id']}")
    print(
        f"  target_type={compact['target_type']} samples={compact['sample_count']} "
        f"deterministic_warnings={compact['deterministic_warning_count']} "
        f"hard_failures={compact['hard_failure_count']}"
    )
    for dimension in result.score.dimensions:
        applicable = "" if dimension.applicable else " (not applicable)"
        print(
            f"    {dimension.dimension.value:24} raw={dimension.raw_score:6.2f} "
            f"weight={dimension.effective_weight:5.2f} "
            f"contribution={dimension.weighted_contribution:6.2f}{applicable}"
        )
    print(
        f"  recomputed_total={result.score.total:.2f} "
        f"pass_threshold={result.score.pass_threshold:.0f} outcome={result.outcome.value}"
    )
    codes = ",".join(code.value for code in result.repair_codes) or "none"
    print(f"  repair_codes={codes}")
    timestamps = [str(value) for value in _evidence_timestamps(result)]
    print("  evidence_timestamps_us=" + (",".join(timestamps) or "none"))
    print(
        f"  first_pass={result.first_pass_provider}/{result.first_pass_model} "
        f"adjudication={compact['adjudication']}"
    )
    print(f"  cost_microusd={result.cost_microusd}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--shot-id", type=UUID, help="evaluate exactly one shot")
    parser.add_argument("--keyframe-only", action="store_true")
    parser.add_argument("--video-only", action="store_true")
    parser.add_argument("--idempotency-key", help="resume an existing run by its key")
    parser.add_argument("--no-adjudication", action="store_true")
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    arguments = parser.parse_args()
    if arguments.keyframe_only and arguments.video_only:
        parser.error("--keyframe-only and --video-only are mutually exclusive")
    options = VisualQACommandOptions(
        provider=arguments.provider,
        idempotency_key=arguments.idempotency_key,
        targets=_targets(arguments),
        shot_id=arguments.shot_id,
        adjudicate=not arguments.no_adjudication,
        openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
        first_pass_model=os.getenv("VIDGEN_VISUAL_QA_FIRST_PASS_MODEL"),
        adjudicator_model=os.getenv("VIDGEN_VISUAL_QA_ADJUDICATOR_MODEL"),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        outcome = await run_visual_qa(
            session, store, project_id=arguments.project_id, options=options
        )
        if arguments.json:
            print(
                json.dumps(
                    {
                        "project_id": str(outcome.project_id),
                        "storyboard_run_id": str(outcome.storyboard_run_id),
                        "results": [_compact(item) for item in outcome.results],
                        "failures": [
                            {"shot_id": str(shot), "target_type": target.value, "code": code}
                            for shot, target, code in outcome.failures
                        ],
                        "status": outcome.status,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        for result in outcome.results:
            _print(result)
        for shot, target, code in outcome.failures:
            print(f"failed shot_id={shot} target_type={target.value} code={code}")
        print(f"status={outcome.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
