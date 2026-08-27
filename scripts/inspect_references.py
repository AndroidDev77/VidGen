"""Inspect persisted T19 references without loading assets or provider payloads."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from vidgen.db.continuity_repository import ContinuityRepository
from vidgen.db.session import build_engine, session_factory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    args = parser.parse_args()
    with session_factory(build_engine())() as session:
        repository = ContinuityRepository(session)
        analysis, storyboard = repository.authoritative_inputs(args.project_id)
        print(
            json.dumps(
                {
                    "project_id": str(args.project_id),
                    "episode_analysis_id": str(analysis.id),
                    "storyboard_run_id": str(storyboard.id),
                    "counts": repository.counts(args.project_id),
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
