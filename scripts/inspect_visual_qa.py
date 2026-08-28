"""Inspect persisted T20 visual-QA results without loading media or provider payloads."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from vidgen.db.session import build_engine, session_factory
from vidgen.db.visual_qa_repository import VisualQARepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--shot-id", type=UUID)
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    arguments = parser.parse_args()
    with session_factory(build_engine())() as session:
        repository = VisualQARepository(session)
        runs = (
            repository.runs_for_shot(arguments.project_id, arguments.shot_id)
            if arguments.shot_id is not None
            else repository.runs_for_project(arguments.project_id)
        )
        rows = []
        for run in runs:
            result = repository.canonical_result(run.id)
            review = repository.latest_human_review(run.id)
            attempts = repository.attempts(run.id)
            rows.append(
                {
                    "qa_run_id": str(run.id),
                    "shot_id": str(run.shot_id),
                    "target_type": run.target_type,
                    "status": run.status,
                    "outcome": run.final_outcome,
                    "recomputed_total": run.final_score,
                    "pass_threshold": run.pass_threshold,
                    "importance": run.importance,
                    "hard_failure": bool(run.hard_failure),
                    "repair_codes": list(run.repair_codes or []),
                    "sample_count": len(repository.samples(run.id)),
                    "attempts": [
                        {
                            "type": attempt.attempt_type,
                            "number": attempt.attempt_number,
                            "provider": attempt.provider,
                            "model": attempt.model,
                            "status": attempt.status,
                        }
                        for attempt in attempts
                    ],
                    "adjudicated": bool(result is not None and result.adjudication),
                    "human_review": review.decision if review is not None else None,
                    "cost_microusd": run.cost_microusd,
                    "rubric_version": run.rubric_version,
                    "threshold_version": run.threshold_version,
                    "sampling_version": run.sampling_version,
                }
            )
        if arguments.json:
            print(json.dumps({"items": rows}, indent=2, sort_keys=True))
            return 0
        for row in rows:
            codes = ",".join(str(code) for code in row["repair_codes"]) or "none"  # type: ignore[union-attr]
            print(
                f"qa_run_id={row['qa_run_id']} shot_id={row['shot_id']} "
                f"target_type={row['target_type']} outcome={row['outcome']} "
                f"score={row['recomputed_total']} threshold={row['pass_threshold']} "
                f"hard_failure={row['hard_failure']} "
                f"repair_codes={codes} "
                f"samples={row['sample_count']} cost_microusd={row['cost_microusd']}"
            )
        print(f"total={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
