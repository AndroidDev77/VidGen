"""The dedicated T25 YouTube publisher worker.

A separate worker, on a separate task queue, for one reason: a resumable upload
can hold an activity slot for hours, and sharing a queue with ordinary project
activities would let one long video starve every other project in the
deployment. Concurrency here is bounded independently and deliberately small.

Deployed as a Container App with **no ingress**: like the T24 Temporal worker it
is reached only by polling Temporal Cloud outbound, so it exposes no port and has
no address. Everything tunable is read from the environment here and nowhere
else, so a revision change is the only way concurrency, the task queue or the
chunk size can move.

Graceful shutdown matters more here than anywhere else in the system. On SIGTERM
the worker stops accepting new activities and gives in-flight ones a real window
to finish the chunk they are sending and commit the confirmed offset; whatever
does not finish resumes from that offset on the next attempt, never from byte
zero.
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

from packages.workflows.publication import PUBLISHER_TASK_QUEUE, YouTubePublicationWorkflow
from packages.workflows.publication_activities import (
    PUBLICATION_ACTIVITIES,
    configure_publication_handler,
)
from vidgen.db.session import build_engine, session_factory
from vidgen.storage.factory import build_blob_store
from vidgen.telemetry.bootstrap import initialize_telemetry
from workers.youtube_publisher.handlers import build_publication_handler

_LOGGER = logging.getLogger("vidgen.publisher.worker")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class _BlobSettings:
    """The structural settings slice :func:`build_blob_store` needs.

    Read from the environment rather than from the API settings module, so the
    publisher worker image does not import the API package to build a store.
    """

    blob_backend = os.getenv("VIDGEN_BLOB_BACKEND", "filesystem")
    blob_root = os.getenv("VIDGEN_BLOB_ROOT", ".local-data/blobs")
    signing_secret = os.getenv("VIDGEN_SIGNING_SECRET", "local-development-only-change-me")
    blob_account_url = os.getenv("VIDGEN_BLOB_ACCOUNT_URL")
    blob_container = os.getenv("VIDGEN_BLOB_CONTAINER", "assets")


async def _connect() -> Client:
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
    initialize_telemetry(service_name="vidgen-publisher")
    factory = session_factory(build_engine(os.getenv("VIDGEN_DATABASE_URL")))
    blob_store = build_blob_store(_BlobSettings())
    configure_publication_handler(build_publication_handler(factory, blob_store))

    client = await _connect()
    task_queue = os.getenv("VIDGEN_YOUTUBE_PUBLISHER_TASK_QUEUE", PUBLISHER_TASK_QUEUE)
    # Small on purpose. Each concurrent activity may hold a multi-gigabyte
    # upload open, and the daily YouTube upload allowance is far smaller than
    # this worker could saturate.
    max_activities = _int_env("VIDGEN_PUBLISHER_MAX_CONCURRENT_UPLOADS", 2)
    activity_threads = _int_env("VIDGEN_PUBLISHER_ACTIVITY_THREADS", max_activities)
    max_workflow_tasks = _int_env("VIDGEN_PUBLISHER_MAX_CONCURRENT_WORKFLOW_TASKS", 20)
    # Kept below the platform's termination grace period so an in-flight chunk
    # can finish and commit its offset instead of vanishing mid-write.
    graceful_seconds = _int_env("VIDGEN_PUBLISHER_GRACEFUL_SHUTDOWN_SECONDS", 60)

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
            workflows=[YouTubePublicationWorkflow],
            activities=PUBLICATION_ACTIVITIES,
            activity_executor=executor,
            max_concurrent_activities=max_activities,
            max_concurrent_workflow_tasks=max_workflow_tasks,
            graceful_shutdown_timeout=timedelta(seconds=graceful_seconds),
        )
        _LOGGER.info(
            "publisher worker starting",
            extra={
                "task_queue": task_queue,
                "namespace": client.namespace,
                "max_concurrent_uploads": max_activities,
            },
        )
        async with worker:
            await stop.wait()
        _LOGGER.info("publisher worker stopped")


def main() -> None:
    logging.basicConfig(level=os.getenv("VIDGEN_LOG_LEVEL", "INFO"))
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
