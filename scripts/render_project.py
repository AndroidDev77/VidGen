"""Validate authoritative T17 inputs and create or reuse the durable render job."""

from __future__ import annotations

import argparse
import json
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from services.renderer.manifest import render_identity
from services.renderer.selection import RenderLineageError, select_authoritative_inputs
from vidgen.db.models import RenderJob
from vidgen.db.render_repository import RenderRepository
from vidgen.db.session import build_engine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--profile", choices=("1080p24", "1080p30"), default="1080p24")
    parser.add_argument(
        "--subtitle-mode", choices=("selectable", "burn_in", "both"), default="selectable"
    )
    parser.add_argument("--ass-burn-in", action="store_true")
    arguments = parser.parse_args()
    try:
        with Session(build_engine()) as session, session.begin():
            selected = select_authoritative_inputs(session, arguments.project_id)
            material = {
                "project_id": str(arguments.project_id),
                "script_id": str(selected.script.id),
                "script_version": selected.script.version,
                "narration_run_id": str(selected.narration.id),
                "storyboard_run_id": str(selected.storyboard.id),
                "timing_manifest_hash": selected.timing_manifest_asset.sha256,
                "shots": [
                    (
                        str(item.shot.stable_shot_id),
                        str(item.asset.id),
                        item.asset.sha256,
                        item.shot.global_start_us,
                        item.shot.global_end_us,
                    )
                    for item in selected.shots
                ],
                "caption_profile": "captions/1",
                "render_profile": arguments.profile,
                "subtitle_mode": "both" if arguments.ass_burn_in else arguments.subtitle_mode,
                "pipeline_version": "t17/1",
            }
            identity = render_identity(material)
            repository = RenderRepository(session)
            completed = repository.completed_by_identity(identity)
            if completed is not None:
                job, reused = completed, True
            else:
                job, reused = repository.create_or_resume(
                    RenderJob(
                        id=uuid4(),
                        project_id=arguments.project_id,
                        status="render_queued",
                        render_identity=identity,
                        idempotency_key=arguments.idempotency_key or f"t17:{identity}",
                        input_hash=identity,
                        script_id=selected.script.id,
                        script_version=selected.script.version,
                        narration_run_id=selected.narration.id,
                        storyboard_run_id=selected.storyboard.id,
                        t16_result_reference=f"t16:{selected.storyboard.id}",
                        expected_duration_us=selected.storyboard.total_duration_us,
                        video_profile={"name": arguments.profile},
                        audio_profile={"name": "aac-48k-320"},
                        caption_profile={"subtitle_mode": material["subtitle_mode"]},
                        pipeline_version="t17/1",
                    )
                )
            print(
                json.dumps(
                    {
                        "render_job_id": str(job.id),
                        "render_identity": identity,
                        "shot_count": len(selected.shots),
                        "expected_duration_us": selected.storyboard.total_duration_us,
                        "status": job.status,
                        "reused": reused,
                    },
                    sort_keys=True,
                )
            )
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


if __name__ == "__main__":
    main()
