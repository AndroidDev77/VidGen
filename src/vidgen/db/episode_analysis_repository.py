from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.episode_analysis_models import (
    EpisodeAnalysisRecord,
    EpisodeAnalysisRun,
    SceneAnalysisCheckpoint,
)


class EpisodeAnalysisRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def run_by_key(self, project_id: UUID, key: str) -> EpisodeAnalysisRun | None:
        return self.session.scalar(
            select(EpisodeAnalysisRun).where(
                EpisodeAnalysisRun.project_id == project_id,
                EpisodeAnalysisRun.idempotency_key == key,
            )
        )

    def checkpoints(self, run_id: UUID) -> dict[UUID, SceneAnalysisCheckpoint]:
        rows = self.session.scalars(
            select(SceneAnalysisCheckpoint).where(SceneAnalysisCheckpoint.analysis_run_id == run_id)
        )
        return {row.source_scene_id: row for row in rows}

    def completed(self, run_id: UUID) -> EpisodeAnalysisRecord | None:
        return self.session.scalar(
            select(EpisodeAnalysisRecord).where(EpisodeAnalysisRecord.analysis_run_id == run_id)
        )

    def next_version(self, project_id: UUID) -> int:
        current = self.session.scalar(
            select(EpisodeAnalysisRecord.version)
            .where(EpisodeAnalysisRecord.project_id == project_id)
            .order_by(EpisodeAnalysisRecord.version.desc())
        )
        return (current or 0) + 1
