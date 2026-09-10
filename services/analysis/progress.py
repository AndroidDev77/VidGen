"""Episode-analysis progress read from the durable T10 checkpoints.

The pipeline persists one ``EpisodeAnalysisRun`` per attempt and one
``SceneAnalysisCheckpoint`` per scene, and moves the run's status through the
scene-map, global-reduce and validation phases as it goes. Those rows are the
only record of where a restartable analysis is, so the progress the owner sees
is derived from them rather than from anything held in memory by a worker.

``calculate_progress`` is pure arithmetic over counts and a status so it can be
tested without a database; ``load_progress`` gathers those inputs for a project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vidgen.db.episode_analysis_models import EpisodeAnalysisRun, SceneAnalysisCheckpoint
from vidgen.db.workflow_models import SceneEvidenceRecord


class EpisodeAnalysisPhase(StrEnum):
    """Where an analysis run is, in the order the pipeline moves through."""

    QUEUED = "queued"
    SCENE_ANALYSIS = "scene_analysis"
    BUILDING_MODEL = "building_model"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


#: The run statuses the pipeline writes, in the order it writes them.
RUN_STATUS_PHASES: dict[str, EpisodeAnalysisPhase] = {
    "episode_analysis_pending": EpisodeAnalysisPhase.QUEUED,
    "episode_scene_mapping": EpisodeAnalysisPhase.SCENE_ANALYSIS,
    "episode_global_reduction": EpisodeAnalysisPhase.BUILDING_MODEL,
    "episode_analysis_validating": EpisodeAnalysisPhase.VALIDATING,
    "episode_analyzed": EpisodeAnalysisPhase.COMPLETED,
    "episode_analysis_failed": EpisodeAnalysisPhase.FAILED,
}

TERMINAL_PHASES: frozenset[EpisodeAnalysisPhase] = frozenset(
    {EpisodeAnalysisPhase.COMPLETED, EpisodeAnalysisPhase.FAILED}
)

#: The scene checkpoint status that counts as done. ``invalid`` and ``failed``
#: checkpoints are retried by the pipeline, so they are still in flight.
COMPLETED_CHECKPOINT_STATUS = "succeeded"

#: Scene analysis is the long, per-scene part of the run and owns most of the
#: bar; the single reduce and validate calls take the rest, so the bar keeps
#: moving after the last scene instead of sitting at the same figure.
SCENE_ANALYSIS_SHARE = 80.0
BUILDING_MODEL_PERCENTAGE = 85.0
VALIDATING_PERCENTAGE = 95.0


@dataclass(frozen=True)
class EpisodeAnalysisProgress:
    phase: EpisodeAnalysisPhase
    completed_scene_count: int
    total_scene_count: int
    #: 0 to 100, at most one decimal place.
    percentage: float
    #: One sentence the dashboard shows next to the bar.
    message: str
    error_code: str | None
    #: When the run or one of its checkpoints last changed.
    updated_at: datetime | None


def calculate_progress(
    *,
    run_status: str,
    completed_scene_count: int,
    total_scene_count: int,
    error_code: str | None = None,
    updated_at: datetime | None = None,
) -> EpisodeAnalysisProgress:
    """Turn a run's status and scene counts into what the dashboard shows.

    A status the pipeline does not write (a future one, or a row edited by hand)
    reads as queued rather than failing the whole status response.
    """
    phase = RUN_STATUS_PHASES.get(run_status, EpisodeAnalysisPhase.QUEUED)
    total = max(total_scene_count, 0)
    completed = min(max(completed_scene_count, 0), total)
    scene_share = SCENE_ANALYSIS_SHARE * completed / total if total else 0.0
    if phase is EpisodeAnalysisPhase.QUEUED:
        percentage, message = 0.0, "Preparing episode analysis"
    elif phase is EpisodeAnalysisPhase.SCENE_ANALYSIS:
        percentage = scene_share
        message = (
            f"Analyzing scene {min(completed + 1, total)} of {total}"
            if total
            else "Analyzing scenes"
        )
    elif phase is EpisodeAnalysisPhase.BUILDING_MODEL:
        percentage, message = BUILDING_MODEL_PERCENTAGE, "Building episode model"
    elif phase is EpisodeAnalysisPhase.VALIDATING:
        percentage, message = VALIDATING_PERCENTAGE, "Validating episode analysis"
    elif phase is EpisodeAnalysisPhase.COMPLETED:
        percentage, message = 100.0, "Episode analysis complete"
    else:
        # The bar keeps the scenes that did finish so a retry is not a restart
        # from zero in the owner's eyes.
        percentage = scene_share
        message = "Episode analysis failed"
        if error_code:
            message = f"{message} ({error_code})"
    return EpisodeAnalysisProgress(
        phase=phase,
        completed_scene_count=completed,
        total_scene_count=total,
        percentage=round(percentage, 1),
        message=message,
        error_code=error_code if phase is EpisodeAnalysisPhase.FAILED else None,
        updated_at=updated_at,
    )


def load_progress(session: Session, project_id: UUID) -> EpisodeAnalysisProgress | None:
    """The progress of the project's most recent analysis run, if it has one.

    The newest run is the one the owner is waiting on: a retry after a failure
    or a fresh workflow start creates a run after the one it supersedes. Before
    the run has written its checkpoints the scene total comes from the evidence
    package it will analyse, so the first message already says how many scenes
    there are.
    """
    run = session.scalar(
        select(EpisodeAnalysisRun)
        .where(EpisodeAnalysisRun.project_id == project_id)
        .order_by(EpisodeAnalysisRun.created_at.desc(), EpisodeAnalysisRun.id.desc())
    )
    if run is None:
        return None
    checkpoints = list(
        session.scalars(
            select(SceneAnalysisCheckpoint).where(SceneAnalysisCheckpoint.analysis_run_id == run.id)
        )
    )
    total = len(checkpoints)
    if total == 0:
        total = (
            session.scalar(
                select(func.count())
                .select_from(SceneEvidenceRecord)
                .where(SceneEvidenceRecord.evidence_package_id == run.evidence_package_id)
            )
            or 0
        )
    completed = sum(
        1 for checkpoint in checkpoints if checkpoint.status == COMPLETED_CHECKPOINT_STATUS
    )
    updated_at = max((checkpoint.updated_at for checkpoint in checkpoints), default=run.updated_at)
    return calculate_progress(
        run_status=run.status,
        completed_scene_count=completed,
        total_scene_count=total,
        error_code=run.error_code,
        updated_at=max(updated_at, run.updated_at),
    )
