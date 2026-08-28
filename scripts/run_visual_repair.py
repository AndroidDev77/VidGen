"""Run or resume the bounded T21 repair for exactly one failed shot.

Fake mode is fully deterministic and needs no paid credential:

    uv run python scripts/run_visual_repair.py PROJECT_UUID SHOT_UUID \
        --provider fake --alternate-provider fake

The configured production providers read their credentials from the
environment:

    RUNWAYML_API_SECRET=... \
    VIDGEN_GOOGLE_CLOUD_PROJECT=... VIDGEN_GOOGLE_ACCESS_TOKEN=... \
    VIDGEN_OPENAI_API_KEY=... \
    uv run python scripts/run_visual_repair.py PROJECT_UUID SHOT_UUID \
        --provider runway --alternate-provider veo

The command repairs one shot. Sibling shots, their checkpoints and their
passing T20 results are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from services.qa.commands import (
    VisualQACommandOptions,
    VisualRepairCommandOptions,
    run_visual_repair,
)
from vidgen.contracts.repair import RepairAttempt, RepairOutcome
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.blob import FilesystemBlobStore


def _lineage(outcome: RepairOutcome) -> list[dict[str, object]]:
    return [
        {
            "ordinal": attempt.lineage.attempt_ordinal,
            "kind": attempt.attempt_kind.value,
            "status": attempt.status.value,
            "predecessor_attempt_id": (
                str(attempt.lineage.predecessor_attempt_id)
                if attempt.lineage.predecessor_attempt_id
                else None
            ),
            "root_animation_attempt_id": str(attempt.lineage.root_animation_attempt_id),
            "provider": attempt.provider,
            "model": attempt.model,
            "provider_operation_id": attempt.provider_operation_id,
            "prompt_hash": attempt.prompt_hash,
            "seed": attempt.seed,
            "output_asset_ids": [str(value) for value in attempt.output_asset_ids],
            "output_qa_result_id": (
                str(attempt.output_qa_result_id) if attempt.output_qa_result_id else None
            ),
            "estimated_cost": str(attempt.estimated_cost),
            "actual_cost": str(attempt.actual_cost),
            "failure_category": (
                attempt.failure_category.value if attempt.failure_category else None
            ),
            "failure_code": attempt.failure_code,
        }
        for attempt in outcome.attempts
    ]


def _compact(outcome: RepairOutcome) -> dict[str, object]:
    classification = outcome.classification
    current = outcome.attempts[-1] if outcome.attempts else None
    route = outcome.decisions[-1].route.value if outcome.decisions else "none"
    return {
        "repair_run_id": str(outcome.repair_run_id),
        "root_animation_attempt_id": str(outcome.root_animation_attempt_id),
        "shot_id": str(outcome.shot_id),
        "state": outcome.state.value,
        "current_attempt_ordinal": current.lineage.attempt_ordinal if current else None,
        "current_attempt_kind": current.attempt_kind.value if current else None,
        "current_route": route,
        "failure_classification": classification.category.value if classification else None,
        "failure_severity": classification.severity.value if classification else None,
        "repair_code": classification.primary_code.value if classification else None,
        "provider": current.provider if current else "",
        "model": current.model if current else "",
        "provider_operation_id": current.provider_operation_id if current else None,
        "qa_score": outcome.final_qa_score,
        "qa_decision": "PASS" if outcome.selected_attempt_id else "not_selected",
        "selected_attempt_id": (
            str(outcome.selected_attempt_id) if outcome.selected_attempt_id else None
        ),
        "selected_output_asset_id": (
            str(outcome.selected_asset_id) if outcome.selected_asset_id else None
        ),
        "final_qa_result_id": (
            str(outcome.final_qa_result_id) if outcome.final_qa_result_id else None
        ),
        "attempt_lineage": _lineage(outcome),
        "cost": {
            "currency": outcome.currency,
            "total_repair_cost": str(outcome.total_repair_cost),
            "estimated_total": str(
                sum((item.estimated_cost for item in outcome.attempts), Decimal("0"))
            ),
        },
        "human_review_reason": (
            outcome.human_review_reason.value if outcome.human_review_reason else None
        ),
        "policy": {
            "version": outcome.policy.policy_version,
            "max_same_provider_repairs": outcome.policy.max_same_provider_repairs,
            "max_alternate_provider_attempts": outcome.policy.max_alternate_provider_attempts,
            "max_fallback_renders": outcome.policy.max_fallback_renders,
        },
        "final_status": outcome.state.value,
    }


def _attempt_line(attempt: RepairAttempt) -> str:
    predecessor = attempt.lineage.predecessor_attempt_id
    return (
        f"    #{attempt.lineage.attempt_ordinal} {attempt.attempt_kind.value:24} "
        f"{attempt.status.value:12} {attempt.provider or '-'}/{attempt.model or '-'} "
        f"cost={attempt.actual_cost} "
        f"predecessor={str(predecessor)[:8] if predecessor else 'root'}"
    )


def _print(outcome: RepairOutcome) -> None:
    compact = _compact(outcome)
    print(f"repair_run_id={compact['repair_run_id']} shot_id={compact['shot_id']}")
    print(f"  root_animation_attempt_id={compact['root_animation_attempt_id']}")
    print(
        f"  current_attempt={compact['current_attempt_ordinal']} "
        f"kind={compact['current_attempt_kind']} route={compact['current_route']}"
    )
    print(
        f"  failure_classification={compact['failure_classification']} "
        f"severity={compact['failure_severity']} repair_code={compact['repair_code']}"
    )
    print(f"  provider={compact['provider']} model={compact['model']}")
    if compact["provider_operation_id"]:
        print(f"  provider_operation_id={compact['provider_operation_id']}")
    print(f"  qa_score={compact['qa_score']} qa_decision={compact['qa_decision']}")
    print(f"  selected_output_asset_id={compact['selected_output_asset_id']}")
    print("  attempt_lineage:")
    for attempt in outcome.attempts:
        print(_attempt_line(attempt))
    cost = compact["cost"]
    assert isinstance(cost, dict)
    print(
        f"  cost total={cost['total_repair_cost']} {cost['currency']} "
        f"estimated={cost['estimated_total']}"
    )
    if compact["human_review_reason"]:
        print(f"  human_review_reason={compact['human_review_reason']}")
    print(f"status={compact['final_status']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("shot_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "runway"), default="fake")
    parser.add_argument("--alternate-provider", choices=("fake", "veo", "none"), default="fake")
    parser.add_argument("--idempotency-key", help="resume an existing repair run by its key")
    parser.add_argument("--max-same-provider-repairs", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--allow-parallax-fallback", action="store_true", default=True)
    parser.add_argument(
        "--no-parallax-fallback",
        dest="allow_parallax_fallback",
        action="store_false",
        help="disable the free deterministic 2.5D fallback",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the existing repair run instead of starting a new one",
    )
    parser.add_argument("--per-shot-repair-cost-limit", type=Decimal, default=None)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--qa-provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    arguments = parser.parse_args()
    options = VisualRepairCommandOptions(
        provider=arguments.provider,
        alternate_provider=arguments.alternate_provider,
        idempotency_key=arguments.idempotency_key,
        max_same_provider_repairs=arguments.max_same_provider_repairs,
        allow_parallax_fallback=arguments.allow_parallax_fallback,
        resume=arguments.resume,
        width=arguments.width,
        height=arguments.height,
        per_shot_repair_cost_limit=arguments.per_shot_repair_cost_limit,
        qa=VisualQACommandOptions(
            provider=arguments.qa_provider,
            openai_api_key=os.getenv("VIDGEN_OPENAI_API_KEY"),
            first_pass_model=os.getenv("VIDGEN_VISUAL_QA_FIRST_PASS_MODEL"),
            adjudicator_model=os.getenv("VIDGEN_VISUAL_QA_ADJUDICATOR_MODEL"),
            expected_width=arguments.width,
            expected_height=arguments.height,
        ),
    )
    store = FilesystemBlobStore(
        Path(os.getenv("VIDGEN_BLOB_ROOT", ".vidgen/blobs")),
        os.getenv("VIDGEN_BLOB_SIGNING_SECRET", "development-only").encode(),
    )
    with session_factory(build_engine())() as session:
        outcome = await run_visual_repair(
            session,
            store,
            project_id=arguments.project_id,
            shot_id=arguments.shot_id,
            options=options,
        )
        if arguments.json:
            print(json.dumps(_compact(outcome), indent=2, sort_keys=True))
        else:
            _print(outcome)
    return 0 if outcome.state.value == "LOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
