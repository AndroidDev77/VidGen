from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from workers.temporal_worker.registry import ACTIVITIES, WORKFLOWS


async def run() -> None:
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "vidgen-projects"),
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run())
