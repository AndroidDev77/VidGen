"""Out-of-band render worker: execute one existing render job, or poll for them.

    uv run python -m workers.render_job.main --render-job-id RENDER_JOB_UUID
    uv run python -m workers.render_job.main --from-env      # VIDGEN_RENDER_JOB_ID
    uv run python -m workers.render_job.main --poll          # bounded claim loop

This process never creates a render job. It executes one that already exists,
through the same :func:`~services.render_execution.execute_render_job` the
Temporal activity and the local CLI call, so an Azure Container Apps Job
execution and a laptop produce identical results and identical database state.

Exit codes: ``0`` for a completed render or an idempotent reuse, ``1`` for a
failure, ``2`` for a usage error, ``3`` for cancellation. A SIGTERM or SIGINT -
which is how Container Apps stops a job replica - requests cancellation, lets
the executor terminate FFmpeg and record durable cancellation state, and exits
nonzero rather than pretending the render finished.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.settings import APISettings, get_settings
from services.render_execution.claims import request_cancellation
from services.render_execution.commands import (
    DEFAULT_WORK_ROOT,
    default_worker_id,
    execute_render_job,
)
from services.render_execution.executor import RenderExecutionError
from vidgen.contracts.render_execution import (
    RenderExecutionResult,
    RenderExecutionStatus,
    RenderWorkerResult,
)
from vidgen.db.models import RenderJob
from vidgen.db.session import build_engine
from vidgen.storage.factory import build_blob_store
from vidgen.telemetry.bootstrap import initialize_telemetry

logger = logging.getLogger("vidgen.render_worker")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3

#: Statuses a polling worker will pick up. A running job with a live lease is
#: skipped by the claim itself, so the poll query stays deliberately simple.
POLLABLE = (
    RenderExecutionStatus.QUEUED.value,
    RenderExecutionStatus.CLAIMING.value,
    RenderExecutionStatus.PREPARING.value,
    RenderExecutionStatus.MANIFEST_READY.value,
    RenderExecutionStatus.RENDERING.value,
    RenderExecutionStatus.VERIFYING.value,
    RenderExecutionStatus.PERSISTING.value,
    "pending",
)


class _Shutdown:
    """Cooperative shutdown flag shared with the signal handlers."""

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._handle)

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        del frame
        self.requested = True
        logger.warning("shutdown requested", extra={"status": "stopping", "signal": signum})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workers.render_job.main",
        description="Execute an existing VidGen render job.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--render-job-id", type=UUID, default=None, help="the render job to execute"
    )
    target.add_argument(
        "--from-env",
        action="store_true",
        help="read the render job id from VIDGEN_RENDER_JOB_ID",
    )
    target.add_argument(
        "--poll",
        action="store_true",
        help="claim and execute queued render jobs until stopped",
    )
    parser.add_argument("--worker-id", default=None, help="override this worker's lease identity")
    parser.add_argument("--work-root", type=Path, default=None, help="temporary render workspace")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--minimum-free-bytes",
        type=int,
        default=None,
        help="refuse to start unless this much temporary storage is free",
    )
    parser.add_argument("--poll-interval-seconds", type=float, default=10.0)
    parser.add_argument(
        "--max-jobs", type=int, default=0, help="in --poll mode, stop after this many jobs"
    )
    return parser


def resolve_render_job_id(
    arguments: argparse.Namespace, parser: argparse.ArgumentParser
) -> UUID | None:
    if arguments.poll:
        return None
    if arguments.from_env:
        raw = os.environ.get("VIDGEN_RENDER_JOB_ID", "").strip()
        if not raw:
            parser.error("--from-env requires VIDGEN_RENDER_JOB_ID to be set")
        try:
            return UUID(raw)
        except ValueError:
            parser.error("VIDGEN_RENDER_JOB_ID is not a valid UUID")
    if arguments.render_job_id is None:
        parser.error("a render job id is required: use --render-job-id, --from-env or --poll")
    job_id: UUID = arguments.render_job_id
    return job_id


def worker_result(result: RenderExecutionResult) -> RenderWorkerResult:
    """Turn an execution result into the compact record and its exit code."""
    if result.status is RenderExecutionStatus.COMPLETE:
        exit_code = EXIT_OK
    elif result.status is RenderExecutionStatus.CANCELLED:
        exit_code = EXIT_CANCELLED
    else:
        exit_code = EXIT_FAILED
    return RenderWorkerResult(
        render_job_id=result.render_job_id,
        status=result.status,
        reused=result.reused,
        exit_code=exit_code,
        final_video_asset_id=result.final_video_asset_id,
        output_sha256=result.output_sha256,
        measured_duration_us=result.measured_duration_us,
        failure_code=result.failure.code if result.failure else None,
        failure_classification=result.failure.classification if result.failure else None,
    )


def run(arguments: argparse.Namespace, settings: APISettings | None = None) -> int:
    configured = settings or get_settings()
    initialize_telemetry(service_name=os.getenv("VIDGEN_SERVICE_NAME", "vidgen-render"))
    engine = build_engine(configured.database_url)
    blob_store = build_blob_store(configured)
    work_root = arguments.work_root or DEFAULT_WORK_ROOT
    worker_id = arguments.worker_id or default_worker_id()
    shutdown = _Shutdown()
    shutdown.install()

    def execute(render_job_id: UUID) -> RenderWorkerResult:
        with Session(engine, expire_on_commit=False) as session:
            if shutdown.requested:
                request_cancellation(session, render_job_id)
                session.commit()
            try:
                result = execute_render_job(
                    session,
                    blob_store,
                    render_job_id,
                    worker_id=worker_id,
                    work_root=work_root,
                    lease_seconds=arguments.lease_seconds,
                    max_attempts=arguments.max_attempts,
                    execution_timeout_seconds=arguments.timeout_seconds,
                    minimum_free_bytes=arguments.minimum_free_bytes,
                )
            except RenderExecutionError as error:
                session.rollback()
                return RenderWorkerResult(
                    render_job_id=render_job_id,
                    status=RenderExecutionStatus.FAILED,
                    exit_code=EXIT_FAILED,
                    failure_code=error.failure.code,
                    failure_classification=error.failure.classification,
                )
        return worker_result(result)

    if not arguments.poll:
        render_job_id = arguments.resolved_render_job_id
        record = execute(render_job_id)
        print(record.model_dump_json())
        return record.exit_code

    completed = 0
    exit_code = EXIT_OK
    while not shutdown.requested:
        with Session(engine, expire_on_commit=False) as session:
            candidate = session.scalar(
                select(RenderJob)
                .where(
                    RenderJob.status.in_(POLLABLE),
                    RenderJob.cancel_requested.is_(False),
                )
                .order_by(RenderJob.created_at, RenderJob.id)
            )
            render_job_id = candidate.id if candidate is not None else None
        if render_job_id is None:
            time.sleep(max(arguments.poll_interval_seconds, 0.1))
            continue
        record = execute(render_job_id)
        print(record.model_dump_json())
        completed += 1
        if record.exit_code != EXIT_OK:
            exit_code = record.exit_code
        if arguments.max_jobs and completed >= arguments.max_jobs:
            break
        # Back off between claims so a permanently failing job cannot become a
        # hot loop against the database.
        time.sleep(max(arguments.poll_interval_seconds, 0.1))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    arguments.resolved_render_job_id = resolve_render_job_id(arguments, parser)
    return run(arguments)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
