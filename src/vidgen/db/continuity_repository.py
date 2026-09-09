"""Project-scoped persistence and authoritative-input selection for T19."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_candidates,
    character_reference_sets,
    location_identity_versions,
    location_reference_candidates,
    location_reference_sets,
    shot_reference_bindings,
)
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord, EpisodeAnalysisRun
from vidgen.db.models import Project
from vidgen.db.storyboard_models import StoryboardRun


@dataclass(frozen=True, slots=True)
class LineageFailure(Exception):
    code: str
    resource: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.resource}: {self.detail}"


class ContinuityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def project(self, project_id: UUID, owner_subject: str | None = None) -> Project:
        statement: Select[tuple[Project]] = select(Project).where(Project.id == project_id)
        if owner_subject is not None:
            statement = statement.where(Project.owner_subject == owner_subject)
        project = self.session.scalar(statement)
        if project is None:
            raise LineageFailure("not_found", "project", "project is missing or not owned")
        return project

    def authoritative_inputs(self, project_id: UUID) -> tuple[EpisodeAnalysisRecord, StoryboardRun]:
        self.project(project_id)
        analyses = list(
            self.session.scalars(
                select(EpisodeAnalysisRecord)
                .join(
                    EpisodeAnalysisRun,
                    EpisodeAnalysisRecord.analysis_run_id == EpisodeAnalysisRun.id,
                )
                .where(
                    EpisodeAnalysisRecord.project_id == project_id,
                    EpisodeAnalysisRecord.selected.is_(True),
                    EpisodeAnalysisRun.status == "episode_analyzed",
                )
            )
        )
        if len(analyses) != 1:
            raise LineageFailure(
                "authoritative_analysis_missing",
                "episode_analysis",
                "exactly one selected completed T10 analysis is required",
            )
        storyboards = list(
            self.session.scalars(
                select(StoryboardRun).where(
                    StoryboardRun.project_id == project_id,
                    StoryboardRun.selected.is_(True),
                    StoryboardRun.status == "storyboard_complete",
                )
            )
        )
        compatible = [value for value in storyboards if value.episode_model_id == analyses[0].id]
        if len(compatible) != 1:
            raise LineageFailure(
                "authoritative_storyboard_missing",
                "storyboard",
                "exactly one selected completed T13 storyboard for the selected "
                "T10 analysis is required",
            )
        return analyses[0], compatible[0]

    def counts(self, project_id: UUID) -> dict[str, int]:
        self.project(project_id)
        project_tables = {
            "character_versions": character_identity_versions,
            "character_reference_sets": character_reference_sets,
            "location_versions": location_identity_versions,
            "location_reference_sets": location_reference_sets,
            "shot_bindings": shot_reference_bindings,
        }
        result = {
            name: int(
                self.session.scalar(
                    select(func.count()).select_from(table).where(table.c.project_id == project_id)
                )
                or 0
            )
            for name, table in project_tables.items()
        }
        for name, candidates, identities in (
            ("character_candidates", character_reference_candidates, character_identity_versions),
            ("location_candidates", location_reference_candidates, location_identity_versions),
        ):
            result[name] = int(
                self.session.scalar(
                    select(func.count())
                    .select_from(candidates)
                    .join(identities, candidates.c.identity_version_id == identities.c.id)
                    .where(identities.c.project_id == project_id)
                )
                or 0
            )
        return result
