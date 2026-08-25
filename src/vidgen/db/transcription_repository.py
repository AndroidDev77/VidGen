from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from vidgen.db.subtitle_models import SubtitleRun
from vidgen.db.transcription_models import (
    SpeakerTurnRecord,
    Transcript,
    TranscriptionChunk,
    TranscriptionRun,
    TranscriptSegmentRecord,
)


class TranscriptionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_run(self, project_id: UUID, idempotency_key: str) -> TranscriptionRun | None:
        return self.session.scalar(
            select(TranscriptionRun).where(
                TranscriptionRun.project_id == project_id,
                TranscriptionRun.idempotency_key == idempotency_key,
            )
        )

    def add_run(self, run: TranscriptionRun) -> TranscriptionRun:
        self.session.add(run)
        self.session.flush()
        return run

    def chunks(self, run_id: UUID) -> list[TranscriptionChunk]:
        return list(
            self.session.scalars(
                select(TranscriptionChunk)
                .where(TranscriptionChunk.run_id == run_id)
                .order_by(TranscriptionChunk.sequence)
            )
        )

    def chunk(self, run_id: UUID, sequence: int) -> TranscriptionChunk | None:
        return self.session.scalar(
            select(TranscriptionChunk).where(
                TranscriptionChunk.run_id == run_id,
                TranscriptionChunk.sequence == sequence,
            )
        )

    def transcript_for_run(self, run_id: UUID) -> Transcript | None:
        return self.session.scalar(select(Transcript).where(Transcript.run_id == run_id))

    def next_version(self, project_id: UUID) -> int:
        current = self.session.scalar(
            select(func.max(Transcript.version)).where(Transcript.project_id == project_id)
        )
        return int(current or 0) + 1

    def select_run_and_transcript(self, run: TranscriptionRun, transcript: Transcript) -> None:
        self.session.execute(
            update(TranscriptionRun)
            .where(TranscriptionRun.project_id == run.project_id)
            .values(selected=False)
        )
        self.session.execute(
            update(SubtitleRun)
            .where(SubtitleRun.project_id == run.project_id)
            .values(selected=False)
        )
        self.session.execute(
            update(Transcript).where(Transcript.project_id == run.project_id).values(selected=False)
        )
        run.selected = True
        transcript.selected = True

    def segments(self, transcript_id: UUID) -> list[TranscriptSegmentRecord]:
        return list(
            self.session.scalars(
                select(TranscriptSegmentRecord)
                .where(TranscriptSegmentRecord.transcript_id == transcript_id)
                .order_by(TranscriptSegmentRecord.sequence)
            )
        )

    def turns(self, transcript_id: UUID) -> list[SpeakerTurnRecord]:
        return list(
            self.session.scalars(
                select(SpeakerTurnRecord)
                .where(SpeakerTurnRecord.transcript_id == transcript_id)
                .order_by(SpeakerTurnRecord.sequence)
            )
        )
