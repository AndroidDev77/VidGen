"""Start T16 fan-out or issue a compact per-shot command."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from apps.api.settings import get_settings
from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from vidgen.contracts.shot_workflow import ProjectShotFanoutInput, ShotWorkflowCommand
from vidgen.db.session import build_engine
from vidgen.db.storyboard_models import StoryboardRun


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--storyboard-run-id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "runway"), default="fake")
    parser.add_argument("--idempotency-key", default="t16-local")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--shot-id", type=UUID)
    parser.add_argument("--child-workflow-id")
    parser.add_argument("--command", choices=("status", "retry", "cancel"))
    parser.add_argument("--command-id", default="t16-cli-command")
    args = parser.parse_args()
    storyboard_run_id = args.storyboard_run_id
    if storyboard_run_id is None:
        with Session(build_engine(get_settings().database_url)) as session:
            storyboard_run_id = session.scalar(
                select(StoryboardRun.id).where(
                    StoryboardRun.project_id == args.project_id,
                    StoryboardRun.selected,
                    StoryboardRun.status == "storyboard_complete",
                )
            )
        if storyboard_run_id is None:
            parser.error("project has no selected complete T13 storyboard")
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
    )
    if args.shot_id is not None:
        if args.child_workflow_id is None or args.command is None:
            parser.error("per-shot operations require --child-workflow-id and --command")
        handle = client.get_workflow_handle(args.child_workflow_id)
        if args.command == "status":
            state = await handle.query(ShotWorkflow.shot_state)
            print(state.model_dump_json(indent=2) if state else "null")
            return
        await handle.signal(
            ShotWorkflow.command,
            ShotWorkflowCommand(
                command_id=args.command_id,
                project_id=args.project_id,
                storyboard_shot_id=args.shot_id,
                command=args.command,
            ),
        )
        print(args.child_workflow_id)
        return
    workflow_id = f"vidgen-shot-fanout-{args.project_id}-{storyboard_run_id}"
    handle = await client.start_workflow(
        ProjectShotFanoutWorkflow.run,
        ProjectShotFanoutInput(
            project_id=args.project_id,
            storyboard_run_id=storyboard_run_id,
            idempotency_key=args.idempotency_key,
            concurrency=args.concurrency,
            t14_configuration_identity=f"{args.provider}-image/1",
            t15_capability_profile_identity=f"{args.provider}-video/1",
        ),
        id=workflow_id,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "vidgen-projects"),
    )
    print(handle.id)
    result = await handle.result()
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
