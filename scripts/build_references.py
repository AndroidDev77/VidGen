"""Queue or resume a bounded T19 reference build using authoritative inputs."""

from __future__ import annotations

import argparse
import json
from uuid import UUID, uuid4

from vidgen.db.continuity_repository import ContinuityRepository
from vidgen.db.session import build_engine, session_factory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--character", type=UUID)
    parser.add_argument("--location", type=UUID)
    args = parser.parse_args()
    key = args.idempotency_key or f"references-{uuid4()}"
    with session_factory(build_engine())() as session:
        repository = ContinuityRepository(session)
        analysis, storyboard = repository.authoritative_inputs(args.project_id)
        counts = repository.counts(args.project_id)
        print(
            json.dumps(
                {
                    "project_id": str(args.project_id),
                    "episode_analysis_id": str(analysis.id),
                    "storyboard_run_id": str(storyboard.id),
                    "provider": args.provider,
                    "idempotency_key": key,
                    "character_id": str(args.character) if args.character else None,
                    "location_id": str(args.location) if args.location else None,
                    "candidate_count": counts["character_candidates"]
                    + counts["location_candidates"],
                    "draft_count": counts["character_reference_sets"]
                    + counts["location_reference_sets"],
                    "affected_shot_count": counts["shot_bindings"],
                    "cost_microusd": 0,
                    "status": "references_awaiting_approval"
                    if counts["shot_bindings"]
                    else "references_queued",
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
