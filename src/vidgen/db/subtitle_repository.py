from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vidgen.db.subtitle_models import SubtitleCandidateRecord, SubtitleRun
from vidgen.db.transcription_models import Transcript, TranscriptionRun


class SubtitleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_run(self, project_id: UUID, idempotency_key: str) -> SubtitleRun | None:
        return self.session.scalar(
            select(SubtitleRun).where(
                SubtitleRun.project_id == project_id,
                SubtitleRun.idempotency_key == idempotency_key,
            )
        )

    def candidates(self, run_id: UUID) -> list[SubtitleCandidateRecord]:
        return list(
            self.session.scalars(
                select(SubtitleCandidateRecord)
                .where(SubtitleCandidateRecord.run_id == run_id)
                .order_by(SubtitleCandidateRecord.sequence)
            )
        )

    def transcript_for_run(self, run_id: UUID) -> Transcript | None:
        return self.session.scalar(select(Transcript).where(Transcript.subtitle_run_id == run_id))

    def select(self, run: SubtitleRun, transcript: Transcript) -> None:
        self.session.execute(
            update(SubtitleRun)
            .where(SubtitleRun.project_id == run.project_id)
            .values(selected=False)
        )
        self.session.execute(
            update(TranscriptionRun)
            .where(TranscriptionRun.project_id == run.project_id)
            .values(selected=False)
        )
        self.session.execute(
            update(Transcript).where(Transcript.project_id == run.project_id).values(selected=False)
        )
        run.selected = True
        transcript.selected = True
