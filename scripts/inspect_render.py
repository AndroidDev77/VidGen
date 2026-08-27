"""Print compact T17 render state without URLs, media, or FFmpeg logs."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import RenderJob
from vidgen.db.session import build_engine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    arguments = parser.parse_args()
    with Session(build_engine()) as session:
        job = session.scalar(
            select(RenderJob)
            .where(RenderJob.project_id == arguments.project_id)
            .order_by(RenderJob.created_at.desc())
        )
        if job is None:
            raise SystemExit("no T17 render job exists for project")
        print(
            json.dumps(
                {
                    "render_job_id": str(job.id),
                    "render_identity": job.render_identity,
                    "status": job.status,
                    "manifest_asset_id": str(job.manifest_asset_id)
                    if job.manifest_asset_id
                    else None,
                    "srt_asset_id": str(job.srt_asset_id) if job.srt_asset_id else None,
                    "webvtt_asset_id": str(job.webvtt_asset_id) if job.webvtt_asset_id else None,
                    "final_video_asset_id": str(job.final_video_asset_id)
                    if job.final_video_asset_id
                    else None,
                    "verification_report_asset_id": str(job.verification_report_asset_id)
                    if job.verification_report_asset_id
                    else None,
                    "expected_duration_us": job.expected_duration_us,
                    "measured_duration_us": job.measured_duration_us,
                    "pipeline_version": job.pipeline_version,
                    "error_code": job.error_code,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
