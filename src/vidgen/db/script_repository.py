from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.script_models import (
    CompressedPlotPlanRecord,
    Script,
    ScriptEditRecord,
    ScriptGenerationRun,
    ScriptReview,
    ScriptSegment,
)


class ScriptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def run_by_key(self, project_id: UUID, key: str) -> ScriptGenerationRun | None:
        return self.session.scalar(
            select(ScriptGenerationRun).where(
                ScriptGenerationRun.project_id == project_id,
                ScriptGenerationRun.idempotency_key == key,
            )
        )

    def selected_plan(self, generation_run_id: UUID) -> CompressedPlotPlanRecord | None:
        return self.session.scalar(
            select(CompressedPlotPlanRecord).where(
                CompressedPlotPlanRecord.generation_run_id == generation_run_id,
                CompressedPlotPlanRecord.selected,
            )
        )

    def next_plan_version(self, generation_run_id: UUID) -> int:
        current = self.session.scalar(
            select(CompressedPlotPlanRecord.version)
            .where(CompressedPlotPlanRecord.generation_run_id == generation_run_id)
            .order_by(CompressedPlotPlanRecord.version.desc())
        )
        return (current or 0) + 1

    def scripts_for_run(self, generation_run_id: UUID) -> list[Script]:
        return list(
            self.session.scalars(
                select(Script)
                .where(Script.generation_run_id == generation_run_id)
                .order_by(Script.version)
            )
        )

    def selected_script(self, project_id: UUID) -> Script | None:
        return self.session.scalar(
            select(Script).where(Script.project_id == project_id, Script.selected)
        )

    def next_script_version(self, project_id: UUID) -> int:
        current = self.session.scalar(
            select(Script.version)
            .where(Script.project_id == project_id)
            .order_by(Script.version.desc())
        )
        return (current or 0) + 1

    def segments(self, script_id: UUID) -> list[ScriptSegment]:
        return list(
            self.session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script_id)
                .order_by(ScriptSegment.sequence)
            )
        )

    def reviews(self, script_id: UUID) -> list[ScriptReview]:
        return list(
            self.session.scalars(
                select(ScriptReview)
                .where(ScriptReview.script_id == script_id)
                .order_by(ScriptReview.review_sequence)
            )
        )

    def next_review_sequence(self, script_id: UUID) -> int:
        current = self.session.scalar(
            select(ScriptReview.review_sequence)
            .where(ScriptReview.script_id == script_id)
            .order_by(ScriptReview.review_sequence.desc())
        )
        return (current or 0) + 1

    def edits_for_review(self, review_id: UUID) -> list[ScriptEditRecord]:
        return list(
            self.session.scalars(
                select(ScriptEditRecord).where(ScriptEditRecord.review_id == review_id)
            )
        )
