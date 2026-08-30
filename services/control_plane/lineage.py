"""Upstream input identities for durable control commands.

Every command records the identity of the material it was calculated against.
The dispatcher re-derives that identity when it claims the command and refuses
to execute if it has moved: an approval, a regeneration or a final-QA run
computed against inputs that have since changed is stale, and executing it would
spend money producing something the owner never asked for.

The identities below are composed only from IDs, versions and content hashes
that are already persisted, so they are reproducible from the database alone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import ControlCommandType
from vidgen.db.continuity_repository import ContinuityRepository, LineageFailure
from vidgen.db.final_editorial_models import FinalEditorialRun
from vidgen.db.models import RenderJob
from vidgen.db.narration_models import NarrationRun
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun
from vidgen.db.transcription_models import Transcript

IDENTITY_VERSION = "control-command-lineage/1"


class LineageUnavailable(RuntimeError):
    """The project cannot currently produce this command's upstream identity."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def _digest(material: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"identity_version": IDENTITY_VERSION, **material},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def continuity_identity(session: Session, project_id: UUID) -> str:
    """The authoritative T10 analysis and T13 storyboard T19 is bound to."""
    try:
        analysis, storyboard = ContinuityRepository(session).authoritative_inputs(project_id)
    except LineageFailure as error:
        raise LineageUnavailable(
            error.code,
            "This project has no authoritative episode analysis and storyboard yet.",
        ) from error
    return _digest(
        {
            "kind": "continuity",
            "episode_analysis_id": analysis.id,
            "storyboard_run_id": storyboard.id,
            "storyboard_input_hash": storyboard.input_hash,
        }
    )


def render_identity(session: Session, project_id: UUID) -> str:
    """The selected inputs a render (or a rerender) would be produced from."""
    storyboard = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )
    if storyboard is None:
        raise LineageUnavailable(
            "storyboard_not_selected",
            "This project has no selected storyboard to render.",
        )
    narration = session.scalar(
        select(NarrationRun).where(
            NarrationRun.project_id == project_id, NarrationRun.selected.is_(True)
        )
    )
    return _digest(
        {
            "kind": "render",
            "storyboard_run_id": storyboard.id,
            "storyboard_input_hash": storyboard.input_hash,
            "script_id": storyboard.script_id,
            "script_version": storyboard.script_version,
            "narration_run_id": narration.id if narration else None,
        }
    )


def selected_render(session: Session, project_id: UUID) -> RenderJob | None:
    return session.scalar(
        select(RenderJob)
        .where(RenderJob.project_id == project_id, RenderJob.selected.is_(True))
        .order_by(RenderJob.created_at.desc())
    )


def final_qa_identity(session: Session, project_id: UUID) -> str:
    """The exact render T22 must inspect. No render, no identity, no command."""
    render = selected_render(session, project_id)
    if render is None or render.status != "render_complete":
        raise LineageUnavailable(
            "render_not_complete",
            "Final QA needs a selected, completed render to inspect.",
        )
    if render.final_video_asset_id is None:
        raise LineageUnavailable(
            "render_asset_missing",
            "The selected render has no final video asset.",
        )
    return _digest(
        {
            "kind": "final_qa",
            "render_job_id": render.id,
            "render_input_hash": render.input_hash,
            "final_video_asset_id": render.final_video_asset_id,
        }
    )


def final_qa_run_identity(session: Session, project_id: UUID, run_id: UUID) -> str:
    """A remediation is bound to one report *and* the render it inspected."""
    run = session.get(FinalEditorialRun, run_id)
    if run is None or run.project_id != project_id:
        raise LineageUnavailable("final_qa_run_not_found", "That final-QA run does not exist.")
    return _digest(
        {
            "kind": "final_qa_run",
            "final_editorial_run_id": run.id,
            "final_render_asset_id": run.final_render_asset_id,
            "status": run.status,
            "decision": run.final_decision,
        }
    )


def transcript_identity(session: Session, project_id: UUID, transcript_id: UUID) -> str:
    transcript = session.get(Transcript, transcript_id)
    if transcript is None or transcript.project_id != project_id:
        raise LineageUnavailable("transcript_not_found", "That transcript does not exist.")
    return _digest(
        {
            "kind": "transcript",
            "transcript_id": transcript.id,
            "version": transcript.version,
            "selected": transcript.selected,
        }
    )


def script_identity(session: Session, project_id: UUID, script_id: UUID) -> str:
    script = session.get(Script, script_id)
    if script is None or script.project_id != project_id:
        raise LineageUnavailable("script_not_found", "That script does not exist.")
    return _digest(
        {
            "kind": "script",
            "script_id": script.id,
            "version": script.version,
            "status": script.status,
        }
    )


def shot_identity(identity_hash: str) -> str:
    """A shot command is bound to the T16 material identity it targets."""
    return _digest({"kind": "shot", "identity_hash": identity_hash})


def project_identity(session: Session, project_id: UUID, entry_stage: str) -> str:
    """A continuation is bound to the stage it resumes and the current lineage."""
    material: dict[str, Any] = {"kind": "project", "entry_stage": entry_stage}
    storyboard = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )
    if storyboard is not None:
        material["storyboard_run_id"] = storyboard.id
        material["storyboard_input_hash"] = storyboard.input_hash
    script = session.scalar(
        select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
    )
    if script is not None:
        material["script_id"] = script.id
        material["script_version"] = script.version
    return _digest(material)


def upstream_identity(
    session: Session,
    *,
    project_id: UUID,
    command_type: ControlCommandType,
    target_id: UUID,
    entry_stage: str = "upload",
    shot_identity_hash: str | None = None,
) -> str:
    """The single entry point every route and the dispatcher both call."""
    match command_type:
        case (
            ControlCommandType.REFERENCE_BUILD
            | ControlCommandType.REFERENCE_GENERATE
            | ControlCommandType.REFERENCE_APPLY
        ):
            return continuity_identity(session, project_id)
        case (
            ControlCommandType.SHOT_REGENERATE
            | ControlCommandType.SHOT_RETRY
            | ControlCommandType.SHOT_REVIEW_CONTINUE
        ):
            if shot_identity_hash is None:
                raise LineageUnavailable(
                    "shot_identity_missing",
                    "A shot command must name the shot workflow identity it targets.",
                )
            return shot_identity(shot_identity_hash)
        case ControlCommandType.FINAL_QA_RUN:
            return final_qa_identity(session, project_id)
        case ControlCommandType.FINAL_QA_REMEDIATION:
            return final_qa_run_identity(session, project_id, target_id)
        case ControlCommandType.RENDER_RERENDER:
            return render_identity(session, project_id)
        case ControlCommandType.TRANSCRIPT_REVISION:
            return transcript_identity(session, project_id, target_id)
        case ControlCommandType.SCRIPT_REVISION:
            return script_identity(session, project_id, target_id)
        case ControlCommandType.PROJECT_CONTINUE:
            return project_identity(session, project_id, entry_stage)
