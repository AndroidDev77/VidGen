"""Authoritative T13 input selection, lineage checks, and restartable checkpoints.

Every rejection raised here is a deterministic lineage or configuration failure.
The pipeline never retries a provider for one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord
from vidgen.db.models import Asset, Project
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.db.storyboard_models import (
    StoryboardRepairAttempt,
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord


class StoryboardLineageError(ValueError):
    """A structured, actionable rejection of stale or incompatible inputs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AuthoritativeInputs:
    project: Project
    episode_model: EpisodeAnalysisRecord
    episode_model_hash: str
    script: Script
    script_segments: tuple[ScriptSegment, ...]
    narration_run: NarrationRun
    narration_segments: tuple[NarrationSegment, ...]
    evidence_package: EvidencePackageRecord | None


class StoryboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- authoritative selection -------------------------------------------------

    def authoritative_inputs(self, project_id: UUID) -> AuthoritativeInputs:
        project = self.session.get(Project, project_id)
        if project is None:
            raise StoryboardLineageError("project_missing", "project does not exist")
        episode_model = self._episode_model(project_id)
        script = self._script(project_id, episode_model)
        script_segments = self._script_segments(script)
        narration_run = self._narration_run(project_id, script)
        narration_segments = self._narration_segments(narration_run, script_segments)
        evidence = self.session.scalar(
            select(EvidencePackageRecord).where(
                EvidencePackageRecord.project_id == project_id,
                EvidencePackageRecord.selected,
            )
        )
        return AuthoritativeInputs(
            project=project,
            episode_model=episode_model,
            episode_model_hash=episode_model.input_hash,
            script=script,
            script_segments=script_segments,
            narration_run=narration_run,
            narration_segments=narration_segments,
            evidence_package=evidence,
        )

    def _episode_model(self, project_id: UUID) -> EpisodeAnalysisRecord:
        record = self.session.scalar(
            select(EpisodeAnalysisRecord).where(
                EpisodeAnalysisRecord.project_id == project_id,
                EpisodeAnalysisRecord.selected,
            )
        )
        if record is None:
            raise StoryboardLineageError(
                "episode_model_unselected", "project has no selected T10 episode model"
            )
        if record.canonical_analysis_asset_id is None:
            raise StoryboardLineageError(
                "episode_model_incomplete", "selected T10 episode model has no canonical asset"
            )
        asset = self.session.get(Asset, record.canonical_analysis_asset_id)
        if asset is None or asset.project_id not in (None, project_id):
            raise StoryboardLineageError(
                "episode_model_cross_project",
                "selected T10 episode model asset is missing or belongs to another project",
            )
        newer = self.session.scalar(
            select(EpisodeAnalysisRecord).where(
                EpisodeAnalysisRecord.project_id == project_id,
                EpisodeAnalysisRecord.version > record.version,
            )
        )
        if newer is not None:
            raise StoryboardLineageError(
                "episode_model_stale",
                "a newer T10 episode model version exists; reselect before storyboarding",
            )
        return record

    def _script(self, project_id: UUID, episode_model: EpisodeAnalysisRecord) -> Script:
        script = self.session.scalar(
            select(Script).where(Script.project_id == project_id, Script.selected)
        )
        if script is None:
            raise StoryboardLineageError("script_unselected", "project has no selected T11 script")
        if script.status != "approved":
            raise StoryboardLineageError(
                "script_unapproved", f"selected T11 script status is {script.status!r}"
            )
        if script.episode_analysis_id != episode_model.id:
            raise StoryboardLineageError(
                "script_lineage_mismatch",
                "the selected T11 script was not generated from the selected T10 episode model",
            )
        return script

    def _script_segments(self, script: Script) -> tuple[ScriptSegment, ...]:
        segments = tuple(
            self.session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script.id)
                .order_by(ScriptSegment.sequence)
            )
        )
        if not segments:
            raise StoryboardLineageError("script_incomplete", "selected T11 script has no segments")
        if [segment.sequence for segment in segments] != list(range(len(segments))):
            raise StoryboardLineageError(
                "script_incomplete", "selected T11 script has a non-dense segment sequence"
            )
        return segments

    def _narration_run(self, project_id: UUID, script: Script) -> NarrationRun:
        run = self.session.scalar(
            select(NarrationRun).where(NarrationRun.project_id == project_id, NarrationRun.selected)
        )
        if run is None:
            raise StoryboardLineageError(
                "narration_unselected", "project has no selected T12 narration run"
            )
        if run.status != "narration_complete":
            raise StoryboardLineageError(
                "narration_incomplete", f"selected T12 narration run status is {run.status!r}"
            )
        if run.script_id != script.id or run.script_version != script.version:
            raise StoryboardLineageError(
                "narration_script_mismatch",
                "the selected T12 narration run was generated from a different approved script "
                f"version (narration script {run.script_id} v{run.script_version}, "
                f"approved script {script.id} v{script.version})",
            )
        return run

    def _narration_segments(
        self, run: NarrationRun, script_segments: tuple[ScriptSegment, ...]
    ) -> tuple[NarrationSegment, ...]:
        rows = tuple(
            self.session.scalars(
                select(NarrationSegment)
                .where(NarrationSegment.narration_run_id == run.id)
                .order_by(NarrationSegment.sequence)
            )
        )
        by_script_segment = {row.script_segment_id: row for row in rows}
        selected: list[NarrationSegment] = []
        for segment in script_segments:
            row = by_script_segment.get(segment.id)
            if row is None:
                raise StoryboardLineageError(
                    "narration_segment_missing",
                    f"script segment {segment.id} has no narration segment in the selected run",
                )
            if row.status != "complete":
                raise StoryboardLineageError(
                    "narration_segment_incomplete",
                    f"narration segment for script segment {segment.id} is {row.status!r}",
                )
            if row.normalized_asset_id is None:
                raise StoryboardLineageError(
                    "narration_segment_missing_asset",
                    f"narration segment {row.id} has no normalized audio asset",
                )
            if row.duration_seconds is None or row.duration_seconds <= 0:
                raise StoryboardLineageError(
                    "narration_duration_unmeasured",
                    f"narration segment {row.id} has no measured ffprobe duration",
                )
            if not row.word_timings:
                raise StoryboardLineageError(
                    "narration_word_timings_missing",
                    f"narration segment {row.id} has no persisted word timings",
                )
            selected.append(row)
        return tuple(selected)

    def evidence_scene_ids(self, evidence_package_id: UUID) -> frozenset[UUID]:
        return frozenset(
            self.session.scalars(
                select(SceneEvidenceRecord.id).where(
                    SceneEvidenceRecord.evidence_package_id == evidence_package_id
                )
            )
        )

    # -- run and checkpoint access -----------------------------------------------

    def run_by_key(self, project_id: UUID, idempotency_key: str) -> StoryboardRun | None:
        return self.session.scalar(
            select(StoryboardRun).where(
                StoryboardRun.project_id == project_id,
                StoryboardRun.idempotency_key == idempotency_key,
            )
        )

    def next_version(self, project_id: UUID) -> int:
        versions = list(
            self.session.scalars(
                select(StoryboardRun.version).where(StoryboardRun.project_id == project_id)
            )
        )
        return max(versions, default=0) + 1

    def checkpoint(self, run_id: UUID, sequence: int) -> StoryboardSegmentCheckpoint | None:
        return self.session.scalar(
            select(StoryboardSegmentCheckpoint).where(
                StoryboardSegmentCheckpoint.storyboard_run_id == run_id,
                StoryboardSegmentCheckpoint.sequence == sequence,
            )
        )

    def checkpoints(self, run_id: UUID) -> list[StoryboardSegmentCheckpoint]:
        return list(
            self.session.scalars(
                select(StoryboardSegmentCheckpoint)
                .where(StoryboardSegmentCheckpoint.storyboard_run_id == run_id)
                .order_by(StoryboardSegmentCheckpoint.sequence)
            )
        )

    def shots(self, run_id: UUID) -> list[StoryboardShotRecord]:
        return list(
            self.session.scalars(
                select(StoryboardShotRecord)
                .where(StoryboardShotRecord.storyboard_run_id == run_id)
                .order_by(StoryboardShotRecord.global_sequence)
            )
        )

    def segment_shots(self, checkpoint_id: UUID) -> list[StoryboardShotRecord]:
        return list(
            self.session.scalars(
                select(StoryboardShotRecord)
                .where(StoryboardShotRecord.segment_checkpoint_id == checkpoint_id)
                .order_by(StoryboardShotRecord.segment_sequence)
            )
        )

    def shot_count_before(self, run_id: UUID, sequence: int) -> int:
        """How many canonical shots precede a segment on the project timeline."""
        earlier = [row.id for row in self.checkpoints(run_id) if row.sequence < sequence]
        if not earlier:
            return 0
        return len(
            list(
                self.session.scalars(
                    select(StoryboardShotRecord.id).where(
                        StoryboardShotRecord.segment_checkpoint_id.in_(earlier)
                    )
                )
            )
        )

    def checkpoints_from(self, run_id: UUID, sequence: int) -> list[StoryboardSegmentCheckpoint]:
        return [row for row in self.checkpoints(run_id) if row.sequence >= sequence]

    def repair_attempts(self, checkpoint_id: UUID) -> list[StoryboardRepairAttempt]:
        return list(
            self.session.scalars(
                select(StoryboardRepairAttempt)
                .where(StoryboardRepairAttempt.segment_checkpoint_id == checkpoint_id)
                .order_by(StoryboardRepairAttempt.attempt_number)
            )
        )

    def repair_attempt_count(self, run_id: UUID) -> int:
        checkpoint_ids = [row.id for row in self.checkpoints(run_id)]
        if not checkpoint_ids:
            return 0
        return len(
            list(
                self.session.scalars(
                    select(StoryboardRepairAttempt.id).where(
                        StoryboardRepairAttempt.segment_checkpoint_id.in_(checkpoint_ids)
                    )
                )
            )
        )

    def deselect_other_runs(self, run: StoryboardRun) -> None:
        self.session.query(StoryboardRun).filter(
            StoryboardRun.project_id == run.project_id,
            StoryboardRun.script_id == run.script_id,
            StoryboardRun.script_version == run.script_version,
            StoryboardRun.narration_run_id == run.narration_run_id,
            StoryboardRun.id != run.id,
            StoryboardRun.selected,
        ).update({"selected": False}, synchronize_session=False)
