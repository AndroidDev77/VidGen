from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.transcription.chunker import CHUNKER_VERSION, ChunkerConfig, create_audio_chunks
from services.transcription.coverage import calculate_coverage
from services.transcription.diarization import reconcile_speakers
from services.transcription.overlap_merge import merge_chunk_words
from vidgen.contracts.transcription import (
    AudioChunk,
    ChunkTranscriptionResult,
    DiarizationRequest,
    DiarizationResult,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionWarning,
    TranscriptSegment,
    TranscriptWord,
)
from vidgen.db.models import Asset, Project, SourceVideo, asset_dependencies
from vidgen.db.transcription_models import (
    SpeakerTurnRecord,
    Transcript,
    TranscriptionChunk,
    TranscriptionRun,
    TranscriptSegmentRecord,
)
from vidgen.db.transcription_repository import TranscriptionRepository
from vidgen.providers.base import TranscriptionProvider
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore


class TranscriptionPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: TranscriptionProvider,
        *,
        chunker_config: ChunkerConfig | None = None,
        minimum_coverage: float = 0.98,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.chunker_config = chunker_config or ChunkerConfig()
        self.minimum_coverage = minimum_coverage
        self.assets = AssetService(session, blob_store)
        self.repository = TranscriptionRepository(session)

    async def process(
        self,
        *,
        project_id: UUID,
        source_video_id: UUID,
        source_audio_asset_id: UUID,
        idempotency_key: str,
        language_hint: str | None = None,
    ) -> TranscriptionResult:
        project = self.session.get(Project, project_id)
        source_video = self.session.get(SourceVideo, source_video_id)
        source_audio = self.session.get(Asset, source_audio_asset_id)
        if (
            project is None
            or source_video is None
            or source_audio is None
            or source_video.project_id != project_id
            or source_audio.project_id != project_id
        ):
            raise ValueError("project, source video, or transcription audio not found")
        if not self._asset_descends_from(source_audio_asset_id, source_video.asset_id):
            raise ValueError("transcription audio does not descend from the source video")
        parameters = {
            "chunker": asdict(self.chunker_config),
            "minimum_coverage": self.minimum_coverage,
            "language_hint": language_hint,
        }
        run = self.repository.get_run(project_id, idempotency_key)
        if run is None:
            run = self.repository.add_run(
                TranscriptionRun(
                    project_id=project_id,
                    source_video_id=source_video_id,
                    source_audio_asset_id=source_audio_asset_id,
                    idempotency_key=idempotency_key,
                    status="transcription_chunking",
                    language=language_hint,
                    chunker_version=CHUNKER_VERSION,
                    provider=self.provider.provider_name,
                    transcription_model=self.provider.transcription_model,
                    diarization_model=self.provider.diarization_model,
                    parameters=parameters,
                )
            )
            self.session.commit()
        elif not self._run_matches_inputs(
            run,
            source_video_id=source_video_id,
            source_audio_asset_id=source_audio_asset_id,
            parameters=parameters,
        ):
            raise ValueError("idempotency key belongs to different transcription inputs")
        completed = self.repository.transcript_for_run(run.id)
        if run.status == "transcribed" and completed is not None:
            return self._result_from_database(
                run, completed, source_video_id, source_audio_asset_id
            )

        try:
            with TemporaryDirectory(prefix="vidgen-transcription-") as temporary:
                workspace = Path(temporary)
                audio_path = workspace / "transcription.wav"
                self.blob_store.copy_to(source_audio.storage_key, audio_path)
                self._status(project, run, "transcription_chunking")
                chunks, voiced, duration = create_audio_chunks(
                    source=audio_path,
                    workspace=workspace,
                    project_id=project_id,
                    parent_audio_asset_id=source_audio_asset_id,
                    parent_sha256=source_audio.sha256,
                    asset_service=self.assets,
                    config=self.chunker_config,
                )
                self._checkpoint_chunk_rows(run, chunks)
                self.session.commit()

                transcription_results: list[ChunkTranscriptionResult] = []
                diarization_results: list[tuple[AudioChunk, DiarizationResult]] = []
                for chunk in chunks:
                    row = self.repository.chunk(run.id, chunk.sequence)
                    if row is None:
                        raise RuntimeError("transcription chunk checkpoint is missing")
                    if row.status == "complete":
                        transcription_results.append(
                            ChunkTranscriptionResult.model_validate(row.provider_result)
                        )
                        diarization_results.append(
                            (chunk, DiarizationResult.model_validate(row.diarization_result))
                        )
                        continue
                    chunk_path = workspace / f"chunk-{chunk.sequence:05d}.flac"
                    row.attempt_count += 1
                    row.status = "transcribing"
                    row.error_code = None
                    self._status(project, run, "transcribing")
                    try:
                        if row.provider_request_id and row.provider_result:
                            transcript = ChunkTranscriptionResult.model_validate(
                                row.provider_result
                            )
                        else:
                            transcript = await self.provider.transcribe(
                                TranscriptionRequest(
                                    idempotency_key=(
                                        f"{idempotency_key}:transcribe:{chunk.sequence}"
                                    ),
                                    chunk=chunk,
                                    language_hint=language_hint,
                                    timestamp_granularity="word",
                                    options={"attempt": row.attempt_count},
                                ),
                                chunk_path,
                            )
                            row.provider_request_id = transcript.provider_request_id
                            row.provider_result = transcript.model_dump(mode="json")
                            row.status = "diarizing"
                            self.session.commit()
                        self._status(project, run, "diarizing")
                        diarization = await self.provider.diarize(
                            DiarizationRequest(
                                idempotency_key=f"{idempotency_key}:diarize:{chunk.sequence}",
                                chunk=chunk,
                                language_hint=language_hint,
                                options={"attempt": row.attempt_count},
                            ),
                            chunk_path,
                        )
                        row.diarization_request_id = diarization.provider_request_ids[0]
                        row.diarization_result = diarization.model_dump(mode="json")
                        row.status = "complete"
                        self.session.commit()
                    except Exception as error:
                        self.session.rollback()
                        row = self.repository.chunk(run.id, chunk.sequence)
                        if row is not None:
                            row.status = "failed"
                            row.error_code = type(error).__name__
                            self.session.commit()
                        raise
                    transcription_results.append(transcript)
                    diarization_results.append((chunk, diarization))

                self._status(project, run, "transcript_merging")
                words, diagnostics = merge_chunk_words(transcription_results)
                coverage = calculate_coverage(
                    voiced,
                    words,
                    minimum_ratio=self.minimum_coverage,
                )
                if not coverage.passed:
                    run.coverage_score = coverage.ratio
                    run.error_code = "insufficient_voiced_coverage"
                    self.session.commit()
                    raise ValueError("transcript covers less than required voiced audio")
                turns, speaker_warnings = reconcile_speakers(
                    diarization_results, duration_seconds=duration
                )
                language, language_warnings = self._canonical_language(
                    language_hint, transcription_results
                )
                segment = TranscriptSegment(
                    sequence=0,
                    start_seconds=words[0].start_seconds,
                    end_seconds=words[-1].end_seconds,
                    text=" ".join(word.text for word in words),
                    confidence=min(
                        (word.confidence for word in words if word.confidence is not None),
                        default=None,
                    ),
                    source_chunk_ids=[chunk.asset_id for chunk in chunks],
                    words=words,
                )
                provider_warnings = [
                    warning for result in transcription_results for warning in result.warnings
                ] + [warning for _, result in diarization_results for warning in result.warnings]
                warnings = (
                    provider_warnings
                    + language_warnings
                    + [
                        TranscriptionWarning(
                            code="overlap_merge",
                            message=(
                                f"chunk {item.chunk_sequence}: removed {item.removed_words} words "
                                f"using {item.method} alignment ({item.confidence:.3f})"
                            ),
                            chunk_sequence=item.chunk_sequence,
                        )
                        for item in diagnostics
                        if item.removed_words
                    ]
                    + speaker_warnings
                )
                transcript_id = uuid4()
                canonical = {
                    "schema_version": "1.0",
                    "project_id": str(project_id),
                    "run_id": str(run.id),
                    "transcript_id": str(transcript_id),
                    "source_video_id": str(source_video_id),
                    "source_audio_asset_id": str(source_audio_asset_id),
                    "language": language,
                    "text": segment.text,
                    "segments": [segment.model_dump(mode="json")],
                    "speaker_turns": [turn.model_dump(mode="json") for turn in turns],
                    "coverage": coverage.model_dump(mode="json"),
                    "warnings": [warning.model_dump(mode="json") for warning in warnings],
                }
                transcript_asset = self.assets.store(
                    content=(json.dumps(canonical, sort_keys=True) + "\n").encode(),
                    kind="json",
                    media_type="application/json",
                    project_id=project_id,
                    parent_asset_ids=(source_audio_asset_id,),
                    provider=self.provider.provider_name,
                    idempotency_key=f"{idempotency_key}:canonical-transcript",
                    generation_parameters={
                        "transcription_model": self.provider.transcription_model,
                        "diarization_model": self.provider.diarization_model,
                        "chunker_version": CHUNKER_VERSION,
                        "chunk_asset_ids": [str(chunk.asset_id) for chunk in chunks],
                    },
                )
                transcript_row = Transcript(
                    id=transcript_id,
                    project_id=project_id,
                    run_id=run.id,
                    version=self.repository.next_version(project_id),
                    language=language,
                    text=segment.text,
                    transcript_asset_id=transcript_asset.id,
                    duration_seconds=duration,
                    coverage_score=coverage.ratio,
                    warnings=[warning.model_dump(mode="json") for warning in warnings],
                )
                self.session.add(transcript_row)
                self.session.flush()
                self.session.add(
                    TranscriptSegmentRecord(
                        transcript_id=transcript_id,
                        sequence=0,
                        start_seconds=segment.start_seconds,
                        end_seconds=segment.end_seconds,
                        text=segment.text,
                        speaker_label=None,
                        confidence=segment.confidence,
                        source_chunk_ids=[str(value) for value in segment.source_chunk_ids],
                        words=[word.model_dump(mode="json") for word in words],
                        provenance={"chunk_asset_ids": [str(chunk.asset_id) for chunk in chunks]},
                    )
                )
                for turn in turns:
                    self.session.add(
                        SpeakerTurnRecord(
                            transcript_id=transcript_id,
                            sequence=turn.sequence,
                            speaker_label=turn.speaker_label,
                            start_seconds=turn.start_seconds,
                            end_seconds=turn.end_seconds,
                            confidence=turn.confidence,
                            source_chunk_ids=[str(value) for value in turn.source_chunk_ids],
                            provider_metadata={"provider": turn.provider, "model": turn.model},
                            alternate_mappings=turn.alternate_labels,
                            warnings=[item.model_dump(mode="json") for item in turn.warnings],
                        )
                    )
                run.coverage_score = coverage.ratio
                run.language = language
                run.error_code = None
                run.status = "transcribed"
                self.repository.select_run_and_transcript(run, transcript_row)
                project.status = "transcribed"
                self.session.commit()
                return TranscriptionResult(
                    project_id=project_id,
                    run_id=run.id,
                    transcript_id=transcript_id,
                    source_video_id=source_video_id,
                    source_audio_asset_id=source_audio_asset_id,
                    transcript_asset_id=transcript_asset.id,
                    status="transcribed",
                    language=language,
                    text=segment.text,
                    segments=[segment],
                    speaker_turns=turns,
                    coverage=coverage,
                    warnings=warnings,
                )
        except Exception as error:
            self.session.rollback()
            project = self.session.get(Project, project_id)
            run = self.repository.get_run(project_id, idempotency_key)
            if project is not None and run is not None:
                project.status = "transcription_failed"
                run.status = "transcription_failed"
                run.error_code = run.error_code or type(error).__name__
                self.session.commit()
            raise

    def _checkpoint_chunk_rows(self, run: TranscriptionRun, chunks: list[AudioChunk]) -> None:
        existing = {row.sequence: row for row in self.repository.chunks(run.id)}
        for chunk in chunks:
            row = existing.get(chunk.sequence)
            if row is None:
                self.session.add(
                    TranscriptionChunk(
                        run_id=run.id,
                        sequence=chunk.sequence,
                        chunk_asset_id=chunk.asset_id,
                        source_start_seconds=chunk.start_seconds,
                        source_end_seconds=chunk.end_seconds,
                        overlap_before_seconds=chunk.overlap_before_seconds,
                        overlap_after_seconds=chunk.overlap_after_seconds,
                        byte_size=chunk.byte_size,
                        sha256=chunk.sha256,
                        status="pending",
                    )
                )
            elif row.chunk_asset_id != chunk.asset_id or row.sha256 != chunk.sha256:
                raise ValueError("chunking parameters changed for an existing transcription run")

    def _asset_descends_from(self, asset_id: UUID, ancestor_id: UUID) -> bool:
        frontier = [asset_id]
        visited: set[UUID] = set()
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            parent_ids = list(
                self.session.scalars(
                    select(asset_dependencies.c.parent_asset_id).where(
                        asset_dependencies.c.asset_id == current
                    )
                )
            )
            if ancestor_id in parent_ids:
                return True
            frontier.extend(parent_id for parent_id in parent_ids if parent_id not in visited)
        return False

    def _run_matches_inputs(
        self,
        run: TranscriptionRun,
        *,
        source_video_id: UUID,
        source_audio_asset_id: UUID,
        parameters: dict[str, object],
    ) -> bool:
        return (
            run.source_video_id == source_video_id
            and run.source_audio_asset_id == source_audio_asset_id
            and run.chunker_version == CHUNKER_VERSION
            and run.provider == self.provider.provider_name
            and run.transcription_model == self.provider.transcription_model
            and run.diarization_model == self.provider.diarization_model
            and run.parameters == parameters
        )

    @staticmethod
    def _canonical_language(
        language_hint: str | None,
        results: list[ChunkTranscriptionResult],
    ) -> tuple[str | None, list[TranscriptionWarning]]:
        if language_hint:
            return language_hint, []
        languages = list(
            dict.fromkeys(result.language for result in results if result.language is not None)
        )
        if not languages:
            return None, []
        if len(languages) == 1:
            return languages[0], []
        return languages[0], [
            TranscriptionWarning(
                code="conflicting_detected_languages",
                message=f"provider returned multiple languages: {', '.join(languages)}",
            )
        ]

    def _status(self, project: Project, run: TranscriptionRun, status: str) -> None:
        project.status = status
        run.status = status
        self.session.commit()

    def _result_from_database(
        self,
        run: TranscriptionRun,
        transcript: Transcript,
        source_video_id: UUID,
        source_audio_asset_id: UUID,
    ) -> TranscriptionResult:
        segments = [
            TranscriptSegment(
                sequence=row.sequence,
                start_seconds=row.start_seconds,
                end_seconds=row.end_seconds,
                text=row.text,
                speaker_label=row.speaker_label,
                confidence=row.confidence,
                source_chunk_ids=[UUID(value) for value in row.source_chunk_ids],
                words=[TranscriptWord.model_validate(word) for word in row.words],
            )
            for row in self.repository.segments(transcript.id)
        ]
        turns = [
            {
                "sequence": row.sequence,
                "speaker_label": row.speaker_label,
                "start_seconds": row.start_seconds,
                "end_seconds": row.end_seconds,
                "confidence": row.confidence,
                "source_chunk_ids": row.source_chunk_ids,
                "provider": row.provider_metadata["provider"],
                "model": row.provider_metadata["model"],
                "alternate_labels": row.alternate_mappings,
                "warnings": row.warnings,
            }
            for row in self.repository.turns(transcript.id)
        ]
        from vidgen.contracts.transcription import SpeakerTurn, TranscriptCoverage

        transcript_asset = self.session.get(Asset, transcript.transcript_asset_id)
        if transcript_asset is None:
            raise ValueError("canonical transcript asset is missing")
        canonical = json.loads(self.blob_store.read(transcript_asset.storage_key))
        coverage = TranscriptCoverage.model_validate(canonical["coverage"])

        return TranscriptionResult(
            project_id=run.project_id,
            run_id=run.id,
            transcript_id=transcript.id,
            source_video_id=source_video_id,
            source_audio_asset_id=source_audio_asset_id,
            transcript_asset_id=transcript.transcript_asset_id,
            status="transcribed",
            language=transcript.language,
            text=transcript.text,
            segments=segments,
            speaker_turns=[SpeakerTurn.model_validate(turn) for turn in turns],
            coverage=coverage,
            warnings=[TranscriptionWarning.model_validate(item) for item in transcript.warnings],
        )
