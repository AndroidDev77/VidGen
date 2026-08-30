"""Queue a T17 render job - and, with ``--execute``, run it here and now.

Queueing and rendering are separate operations, and this command says which one
it performed. By default it only validates the project's authoritative inputs
and creates (or reuses) the durable render-job row; nothing is rendered until a
worker executes that job:

    uv run python -m scripts.render_project PROJECT_UUID
    uv run python -m workers.render_job.main --render-job-id RENDER_JOB_UUID

``--execute`` runs the canonical executor in this process instead, which is the
convenient path for local development:

    uv run python -m scripts.render_project PROJECT_UUID --execute

``--output-id-only`` prints just the render job id, for shell pipelines.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.settings import get_settings
from services.render_execution.commands import execute_render_job, queue_render_job
from services.render_execution.executor import RenderExecutionError
from services.renderer.selection import RenderLineageError
from vidgen.contracts.render_execution import RenderExecutionStatus
from vidgen.db.session import build_engine
from vidgen.storage.factory import build_blob_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.render_project",
        description="Queue a render job for a project, optionally executing it in process.",
    )
    # The deployed job is project-neutral: the project is supplied per execution
    # with `az containerapp job start --env-vars VIDGEN_PROJECT_ID=...`, so the
    # job definition never has to be redeployed to render a project.
    parser.add_argument("project_id", type=UUID, nargs="?", default=None)
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="read the project id from VIDGEN_PROJECT_ID instead of the positional argument",
    )
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--profile", choices=("1080p24", "1080p30"), default="1080p24")
    parser.add_argument(
        "--subtitle-mode", choices=("selectable", "burn_in", "both"), default="selectable"
    )
    parser.add_argument("--language", default="en")
    parser.add_argument(
        "--ass-burn-in",
        action="store_true",
        help="shorthand for --subtitle-mode both",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="execute the queued render job in this process instead of only queueing it",
    )
    parser.add_argument("--work-root", type=Path, default=None)
    parser.add_argument(
        "--output-id-only",
        action="store_true",
        help="print only the render job id, for shell pipelines",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.from_env:
        raw = os.environ.get("VIDGEN_PROJECT_ID", "").strip()
        if not raw:
            parser.error("--from-env requires VIDGEN_PROJECT_ID to be set")
        arguments.project_id = UUID(raw)
    elif arguments.project_id is None:
        parser.error("a project id is required unless --from-env is given")
    settings = get_settings()
    engine = build_engine(settings.database_url)
    subtitle_mode = "both" if arguments.ass_burn_in else arguments.subtitle_mode
    try:
        with Session(engine, expire_on_commit=False) as session:
            queued = queue_render_job(
                session,
                arguments.project_id,
                profile=arguments.profile,
                subtitle_mode=subtitle_mode,
                language=arguments.language,
                idempotency_key=arguments.idempotency_key,
            )
            payload = {
                "action": "queued",
                "render_job_id": str(queued.job.id),
                "input_hash": queued.input_hash,
                "shot_count": queued.shot_count,
                "expected_duration_us": queued.expected_duration_us,
                "status": queued.job.status,
                "reused": queued.reused,
                "executed": False,
            }
            session.commit()
            render_job_id = queued.job.id
        if arguments.execute:
            blob_store = build_blob_store(settings)
            with Session(engine, expire_on_commit=False) as session:
                result = execute_render_job(
                    session,
                    blob_store,
                    render_job_id,
                    work_root=arguments.work_root,
                )
            payload.update(
                {
                    "action": "executed",
                    "executed": True,
                    "status": result.status.value,
                    "reused": result.reused,
                    "final_video_asset_id": str(result.final_video_asset_id)
                    if result.final_video_asset_id
                    else None,
                    "output_sha256": result.output_sha256,
                    "measured_duration_us": result.measured_duration_us,
                    "failure_code": result.failure.code if result.failure else None,
                }
            )
            if result.status is not RenderExecutionStatus.COMPLETE:
                print(json.dumps(payload, sort_keys=True))
                return 1
    except RenderLineageError as error:
        raise SystemExit(
            json.dumps(
                {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "reference_id": str(error.reference_id) if error.reference_id else None,
                },
                sort_keys=True,
            )
        ) from error
    except RenderExecutionError as error:
        raise SystemExit(
            json.dumps(
                {
                    "code": error.failure.code,
                    "message": error.failure.message,
                    "retryable": error.failure.retryable,
                    "classification": error.failure.classification,
                },
                sort_keys=True,
            )
        ) from error
    if arguments.output_id_only:
        print(payload["render_job_id"])
    else:
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
