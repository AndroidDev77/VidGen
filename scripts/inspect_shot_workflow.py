"""Inspect compact T16 parent state."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from packages.workflows.shot import ProjectShotFanoutWorkflow


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--storyboard-run-id", type=UUID, required=True)
    args = parser.parse_args()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
    )
    workflow_id = f"vidgen-shot-fanout-{args.project_id}-{args.storyboard_run_id}"
    state = await client.get_workflow_handle(workflow_id).query(
        ProjectShotFanoutWorkflow.fanout_state
    )
    print(state.model_dump_json(indent=2) if state else "null")


if __name__ == "__main__":
    asyncio.run(main())
