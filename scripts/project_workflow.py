"""Start or inspect a project workflow without transferring media payloads."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from packages.workflows.project import ProjectWorkflow
from vidgen.contracts.workflow import ProjectWorkflowInput


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "inspect", "cancel"))
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--source-video-id", type=UUID)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    workflow_id = f"vidgen-project-{args.project_id}"
    if args.action == "start":
        if args.source_video_id is None or args.idempotency_key is None:
            parser.error("start requires --source-video-id and --idempotency-key")
        await client.start_workflow(
            ProjectWorkflow.run,
            ProjectWorkflowInput(
                project_id=args.project_id,
                source_video_id=args.source_video_id,
                idempotency_key=args.idempotency_key,
            ),
            id=workflow_id,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "vidgen-projects"),
        )
        print(workflow_id)
        return
    handle = client.get_workflow_handle(workflow_id)
    if args.action == "cancel":
        await handle.signal(ProjectWorkflow.cancel_project)
        return
    state = await handle.query(ProjectWorkflow.project_state)
    print(state.model_dump_json(indent=2) if state is not None else "null")


if __name__ == "__main__":
    asyncio.run(main())
