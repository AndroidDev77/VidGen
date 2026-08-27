from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from packages.workflows.activities import configure_activity_handlers
from packages.workflows.shot_activities import configure_shot_activity_handlers
from workers.temporal_worker.production_handlers import (
    build_production_handlers,
    build_shot_production_handlers,
)
from workers.temporal_worker.registry import ACTIVITIES, WORKFLOWS


async def run() -> None:
    configure_activity_handlers(build_production_handlers())
    configure_shot_activity_handlers(build_shot_production_handlers())
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    max_workers = int(os.getenv("TEMPORAL_ACTIVITY_THREADS", "4"))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        worker = Worker(
            client,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "vidgen-projects"),
            workflows=WORKFLOWS,
            activities=ACTIVITIES,
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(run())
