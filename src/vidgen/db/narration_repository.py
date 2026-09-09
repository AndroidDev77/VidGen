"""Queries and idempotent checkpoints for T12 narration."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.narration_models import (
    NarrationAttemptRecord,
    NarrationRun,
    NarrationSegment,
    VoiceProfileRecord,
)
from vidgen.db.script_models import Script, ScriptSegment


class NarrationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authoritative_script(self, project_id: UUID) -> tuple[Script, list[ScriptSegment]]:
        script = self.session.scalar(
            select(Script).where(Script.project_id == project_id, Script.selected)
        )
        if script is None:
            raise ValueError("project has no selected T11 script")
        if script.status != "approved":
            raise ValueError("selected T11 script is not approved")
        segments = list(
            self.session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script.id)
                .order_by(ScriptSegment.sequence)
            )
        )
        seqs = [s.sequence for s in segments]
        if not segments or seqs != list(range(seqs[0], seqs[0] + len(seqs))):
            raise ValueError("selected T11 script is incomplete")
        if any(not s.text.strip() for s in segments):
            raise ValueError("selected T11 script contains empty segments")
        return script, segments

    def voice_profile(self, profile_id: UUID, project_id: UUID) -> VoiceProfileRecord:
        row = self.session.get(VoiceProfileRecord, profile_id)
        if row is None or row.project_id not in (None, project_id):
            raise ValueError("voice profile is missing or cross-project")
        return row

    def run_by_key(self, project_id: UUID, key: str) -> NarrationRun | None:
        return self.session.scalar(
            select(NarrationRun).where(
                NarrationRun.project_id == project_id, NarrationRun.idempotency_key == key
            )
        )

    def segment_by_identity(self, run_id: UUID, identity: str) -> NarrationSegment | None:
        return self.session.scalar(
            select(NarrationSegment).where(
                NarrationSegment.narration_run_id == run_id,
                NarrationSegment.generation_identity == identity,
            )
        )

    def reusable_segment(self, identity: str) -> NarrationSegment | None:
        return self.session.scalar(
            select(NarrationSegment)
            .where(
                NarrationSegment.generation_identity == identity,
                NarrationSegment.status == "complete",
            )
            .order_by(NarrationSegment.created_at.desc())
        )

    def attempts(self, segment_id: UUID) -> list[NarrationAttemptRecord]:
        return list(
            self.session.scalars(
                select(NarrationAttemptRecord)
                .where(NarrationAttemptRecord.narration_segment_id == segment_id)
                .order_by(NarrationAttemptRecord.attempt_number)
            )
        )
