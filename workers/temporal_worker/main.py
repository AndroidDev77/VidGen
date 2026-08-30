"""Production Temporal worker entry point.

The worker is deployed as a non-ingress Container App. It polls Temporal Cloud
task queues continuously, so it is never scaled to zero and is never expressed
as a finite Container Apps Job: a worker that is not polling is a workflow that
does not progress.

Everything the deployment needs to tune is read from the environment here and
nowhere else, so a revision change is the only way concurrency, the task queue
or the Temporal endpoint can move.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from packages.workflows.activities import (
    configure_activity_handlers,
    configure_final_qa_handler,
    configure_render_handler,
)
from packages.workflows.project import RENDER_TASK_QUEUE
from packages.workflows.shot_activities import configure_shot_activity_handlers
from vidgen.telemetry.bootstrap import initialize_telemetry
from workers.temporal_worker.production_handlers import (
    build_final_qa_handler,
    build_production_handlers,
    build_render_handler,
    build_shot_production_handlers,
)
from workers.temporal_worker.registry import ACTIVITIES, RENDER_ACTIVITIES, WORKFLOWS

_LOGGER = logging.getLogger("vidgen.worker")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


async def _connect() -> Client:
    """Connect to the configured Temporal endpoint.

    Temporal Cloud is the deployed target: TLS is on and the API key is
    resolved from Key Vault into ``TEMPORAL_API_KEY`` by the Container App's
    managed identity. A local ``temporal server start-dev`` has neither, so the
    plaintext path stays available for development and integration tests.
    """
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY") or None
    tls_default = "true" if api_key else "false"
    tls_enabled = os.getenv("TEMPORAL_TLS_ENABLED", tls_default).lower() == "true"
    return await Client.connect(
        address,
        namespace=namespace,
        api_key=api_key,
        tls=TLSConfig() if tls_enabled else False,
        data_converter=pydantic_data_converter,
    )


async def run() -> None:
    initialize_telemetry(service_name="vidgen-worker")
    configure_activity_handlers(build_production_handlers())
    configure_shot_activity_handlers(build_shot_production_handlers())
    configure_final_qa_handler(build_final_qa_handler())
    configure_render_handler(build_render_handler())

    client = await _connect()
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "vidgen-projects")
    activity_threads = _int_env("TEMPORAL_ACTIVITY_THREADS", 4)
    # Bounded independently of the thread pool: activities are the work that
    # touches providers, PostgreSQL and Blob Storage, so their concurrency is
    # what has to respect the connection and provider limits documented in
    # infra/README.md, not the workflow task concurrency.
    max_activities = _int_env("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", activity_threads)
    max_workflow_tasks = _int_env("TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS", 100)
    # Container Apps sends SIGTERM and then waits before SIGKILL. Keep this
    # below that grace period so in-flight activities are given a real chance
    # to finish or to heartbeat a cancellation instead of vanishing mid-write.
    graceful_seconds = _int_env("TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS", 30)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(received, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            pass

    with ThreadPoolExecutor(max_workers=activity_threads) as executor:
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=WORKFLOWS,
            activities=ACTIVITIES,
            activity_executor=executor,
            max_concurrent_activities=max_activities,
            max_concurrent_workflow_tasks=max_workflow_tasks,
            graceful_shutdown_timeout=timedelta(seconds=graceful_seconds),
        )
        # T17b rendering polls its own queue. An encode is CPU, memory and disk
        # bound and runs for minutes; sharing the project queue's concurrency
        # budget with it would starve the provider-bound activities that are
        # mostly waiting on the network. Its concurrency is separately bounded
        # because each concurrent render holds a CPU, a working directory and a
        # PostgreSQL connection.
        render_task_queue = os.getenv("TEMPORAL_RENDER_TASK_QUEUE", RENDER_TASK_QUEUE)
        max_renders = _int_env("TEMPORAL_MAX_CONCURRENT_RENDERS", 1)
        render_worker = Worker(
            client,
            task_queue=render_task_queue,
            activities=RENDER_ACTIVITIES,
            activity_executor=executor,
            max_concurrent_activities=max_renders,
            graceful_shutdown_timeout=timedelta(seconds=graceful_seconds),
        )
        _LOGGER.info(
            "worker starting",
            extra={
                "task_queue": task_queue,
                "render_task_queue": render_task_queue,
                "namespace": client.namespace,
                "max_concurrent_activities": max_activities,
                "max_concurrent_renders": max_renders,
            },
        )
        # `async with` runs the worker and, on exit, performs the graceful
        # shutdown the timeout above bounds. Waiting on the signal event inside
        # the block is what turns a Container Apps SIGTERM into that shutdown
        # rather than an abrupt cancellation of in-flight activities.
        async with worker, render_worker:
            await stop.wait()
            _LOGGER.info("shutdown signal received; draining in-flight activities")
    _LOGGER.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
