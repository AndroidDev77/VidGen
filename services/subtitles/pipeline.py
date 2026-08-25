from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import httpx
from sqlalchemy.orm import Session

from services.media_worker.commands import MediaCommandError
from services.media_worker.probe import probe_media
from services.subtitles.embedded import (
    discover_embedded_subtitles,
    extract_embedded_subtitle,
)
from services.subtitles.movie_hash import opensubtitles_movie_hash
from services.subtitles.parser import parse_subtitles
from services.subtitles.providers import SubtitleProvider
from services.subtitles.quality import candidate_sort_key, score_subtitle
from services.subtitles.sync import synchronize_subtitle
from services.transcription.chunker import voiced_intervals
from services.transcription.commands import detect_silence_ranges, probe_duration
from vidgen.contracts.subtitles import (
    CanonicalSubtitleTranscriptArtifact,
    SubtitleCandidate,
    SubtitleCue,
    SubtitleImportResult,
    SubtitleQuality,
    SubtitleSearchRequest,
)
from vidgen.contracts.transcription import (
    TimeInterval,
    TranscriptCoverage,
    TranscriptionWarning,
    TranscriptSegment,
)
from vidgen.db.models import Asset, Project, SourceVideo
from vidgen.db.subtitle_models import SubtitleCandidateRecord, SubtitleRun
from vidgen.db.subtitle_repository import SubtitleRepository
from vidgen.db.transcription_models import Transcript, TranscriptSegmentRecord
from vidgen.db.transcription_repository import TranscriptionRepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore


class SubtitleUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SubtitlePipelineConfig:
    languages: tuple[str, ...] = ("en",)
    minimum_quality_score: float = 0.55
    synchronize_provider_subtitles: bool = False
    allow_provider_search: bool = True
    allow_forced_subtitles: bool = False

    def __post_init__(self) -> None:
        if not self.languages:
            raise ValueError("at least one subtitle language is required")
        if not 0 <= self.minimum_quality_score <= 1:
            raise ValueError("minimum subtitle quality must be between zero and one")


class SubtitlePipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: SubtitleProvider | None = None,
        *,
        config: SubtitlePipelineConfig | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.config = config or SubtitlePipelineConfig()
        self.assets = AssetService(session, blob_store)
        self.repository = SubtitleRepository(session)
        self.transcripts = TranscriptionRepository(session)

    async def process(
        self,
        *,
        project_id: UUID,
        source_video_id: UUID,
        idempotency_key: str,
        source_audio_asset_id: UUID | None = None,
        sidecar_asset_ids: tuple[UUID, ...] = (),
        query: str | None = None,
        imdb_id: str | None = None,
    ) -> SubtitleImportResult:
        project = self.session.get(Project, project_id)
        source_video = self.session.get(SourceVideo, source_video_id)
        if project is None or source_video is None or source_video.project_id != project_id:
            raise ValueError("project or source video not found")
        source_asset = self.session.get(Asset, source_video.asset_id)
        source_audio = (
            self.session.get(Asset, source_audio_asset_id)
            if source_audio_asset_id is not None
            else None
        )
        if source_asset is None or (
            source_audio_asset_id is not None
            and (source_audio is None or source_audio.project_id != project_id)
        ):
            raise ValueError("source video or audio asset not found")
        sidecars = [self.session.get(Asset, asset_id) for asset_id in sidecar_asset_ids]
        if any(asset is None or asset.project_id != project_id for asset in sidecars):
            raise ValueError("sidecar subtitle asset not found")

        parameters: dict[str, object] = json.loads(
            json.dumps(
                {
                    "config": asdict(self.config),
                    "source_audio_asset_id": str(source_audio_asset_id)
                    if source_audio_asset_id
                    else None,
                    "sidecar_asset_ids": [str(value) for value in sidecar_asset_ids],
                    "query": query,
                    "imdb_id": imdb_id,
                }
            )
        )
        run = self.repository.get_run(project_id, idempotency_key)
        if run is None:
            run = SubtitleRun(
                project_id=project_id,
                source_video_id=source_video_id,
                source_audio_asset_id=source_audio_asset_id,
                idempotency_key=idempotency_key,
                status="subtitle_discovery",
                acquisition_mode="automatic",
                provider=self.provider.provider_name if self.provider else None,
                parameters=parameters,
            )
            self.session.add(run)
            self.session.commit()
        elif not self._matches(run, source_video_id, parameters):
            raise ValueError("idempotency key belongs to different subtitle inputs")
        completed = self.repository.transcript_for_run(run.id)
        if run.status == "subtitle_imported" and completed is not None:
            return self._result_from_asset(run, completed)

        try:
            with TemporaryDirectory(prefix="vidgen-subtitles-") as directory:
                workspace = Path(directory)
                video_path = workspace / "source-video"
                self.blob_store.copy_to(source_asset.storage_key, video_path)
                duration = source_video.duration_seconds
                if duration is None:
                    duration = probe_media(video_path).duration_seconds
                voiced = self._voiced_intervals(source_audio, workspace)
                warnings: list[TranscriptionWarning] = []
                candidates, discovery_warnings = discover_embedded_subtitles(video_path)
                warnings.extend(
                    TranscriptionWarning(code="subtitle_stream_ignored", message=message)
                    for message in discovery_warnings
                )
                candidates.extend(self._sidecar_candidates(sidecars))
                local_may_be_usable = any(
                    self.config.allow_forced_subtitles or not candidate.forced
                    for candidate in candidates
                )
                if (
                    self.provider
                    and self.config.allow_provider_search
                    and not local_may_be_usable
                ):
                    self._status(project, run, "subtitle_searching")
                    title, season, episode = _media_identity(query or source_video.filename)
                    request = SubtitleSearchRequest(
                        idempotency_key=f"{idempotency_key}:search",
                        movie_hash=opensubtitles_movie_hash(video_path),
                        byte_size=source_asset.byte_size,
                        query=title,
                        imdb_id=imdb_id,
                        season_number=season,
                        episode_number=episode,
                        languages=list(self.config.languages),
                    )
                    candidates.extend(await self.provider.search(request))
                candidates = _deduplicate_candidates(candidates)
                self._checkpoint_candidates(run, candidates)
                self.session.commit()
                if not candidates:
                    raise SubtitleUnavailableError("no subtitle candidates were found")

                self._status(project, run, "subtitle_validating")
                best: (
                    tuple[
                        SubtitleCandidate,
                        SubtitleCandidateRecord,
                        Asset,
                        list[SubtitleCue],
                        SubtitleQuality,
                    ]
                    | None
                ) = None
                ordered = sorted(
                    candidates,
                    key=lambda item: candidate_sort_key(item, self.config.languages),
                    reverse=True,
                )
                rows = {row.candidate_id: row for row in self.repository.candidates(run.id)}
                for candidate in ordered:
                    row = rows[candidate.candidate_id]
                    try:
                        material = await self._materialize_candidate(
                            candidate,
                            row,
                            video_path,
                            workspace,
                            source_asset,
                            idempotency_key,
                        )
                        content = self.blob_store.read(material.storage_key)
                        cues = parse_subtitles(content, _format(candidate, material))
                        sync_offset = None
                        sync_correlation = None
                        if (
                            candidate.source_type == "provider"
                            and self.config.synchronize_provider_subtitles
                        ):
                            source_sub = workspace / f"{candidate.candidate_id}.srt"
                            source_sub.write_bytes(content)
                            synced = synchronize_subtitle(
                                video_path,
                                source_sub,
                                workspace / f"{candidate.candidate_id}.synced.srt",
                            )
                            sync_offset = synced.offset_seconds
                            sync_correlation = synced.correlation
                            synced_asset = self.assets.store_file(
                                path=synced.path,
                                kind="subtitle",
                                media_type="application/x-subrip",
                                project_id=project_id,
                                parent_asset_ids=(material.id, source_asset.id),
                                provider="ffsubsync",
                                idempotency_key=f"{idempotency_key}:sync:{candidate.candidate_id}",
                                generation_parameters={
                                    "offset_seconds": sync_offset,
                                    "correlation": sync_correlation,
                                },
                            )
                            synced_material = self.session.get(Asset, synced_asset.id)
                            if synced_material is None:
                                raise RuntimeError("synchronized subtitle asset is missing")
                            material = synced_material
                            cues = parse_subtitles(
                                self.blob_store.read(material.storage_key), "srt"
                            )
                        quality = score_subtitle(
                            candidate,
                            cues,
                            duration_seconds=duration,
                            requested_languages=self.config.languages,
                            voiced=voiced,
                            sync_offset_seconds=sync_offset,
                            sync_correlation=sync_correlation,
                            minimum_score=self.config.minimum_quality_score,
                            allow_forced=self.config.allow_forced_subtitles,
                        )
                        row.asset_id = material.id
                        row.score = quality.score
                        row.quality = quality.model_dump(mode="json")
                        row.status = "accepted" if quality.passed else "rejected"
                        self.session.commit()
                        if quality.passed and (best is None or quality.score > best[4].score):
                            best = (candidate, row, material, cues, quality)
                    except (ValueError, MediaCommandError, httpx.HTTPError) as error:
                        self.session.rollback()
                        row = rows[candidate.candidate_id]
                        row.status = "failed"
                        row.error_code = type(error).__name__
                        self.session.commit()
                if best is None:
                    raise SubtitleUnavailableError(
                        "no subtitle candidate passed quality validation"
                    )
                candidate, row, subtitle_asset, cues, quality_value = best
                quality = quality_value
                row.selected = True
                run.selected_candidate_id = candidate.candidate_id
                run.quality_score = quality.score
                run.coverage_score = (
                    quality.voiced_coverage
                    if quality.voiced_coverage is not None
                    else quality.timeline_coverage
                )
                self._status(project, run, "subtitle_importing")
                result = self._persist_transcript(
                    project,
                    source_video,
                    run,
                    candidate,
                    subtitle_asset,
                    cues,
                    quality,
                    duration,
                    voiced,
                    warnings,
                    idempotency_key,
                )
                self.session.commit()
                return result
        except Exception as error:
            self.session.rollback()
            project = self.session.get(Project, project_id)
            run = self.repository.get_run(project_id, idempotency_key)
            if project is not None and run is not None:
                unavailable = isinstance(error, SubtitleUnavailableError)
                run.status = "subtitle_unavailable" if unavailable else "subtitle_failed"
                run.error_code = type(error).__name__
                project.status = run.status
                self.session.commit()
            raise

    def _sidecar_candidates(self, assets: list[Asset | None]) -> list[SubtitleCandidate]:
        result: list[SubtitleCandidate] = []
        for asset in assets:
            assert asset is not None
            filename = str(asset.extra_metadata.get("filename") or f"{asset.id}.srt")
            subtitle_format = _format_from_name_or_media(filename, asset.media_type)
            result.append(
                SubtitleCandidate(
                    candidate_id=f"sidecar_{asset.id}",
                    source_type="sidecar",
                    provider="user-upload",
                    asset_id=asset.id,
                    language=_language_from_filename(filename),
                    subtitle_format=subtitle_format,
                    file_name=filename,
                )
            )
        return result

    async def _materialize_candidate(
        self,
        candidate: SubtitleCandidate,
        row: SubtitleCandidateRecord,
        video_path: Path,
        workspace: Path,
        source_asset: Asset,
        idempotency_key: str,
    ) -> Asset:
        if row.asset_id is not None:
            existing = self.session.get(Asset, row.asset_id)
            if existing is not None:
                return existing
        if candidate.source_type == "embedded":
            output = extract_embedded_subtitle(
                video_path, candidate, workspace / f"{candidate.candidate_id}.vtt"
            )
            stored = self.assets.store_file(
                path=output,
                kind="subtitle",
                media_type="text/vtt",
                project_id=source_asset.project_id,
                parent_asset_ids=(source_asset.id,),
                provider="ffmpeg",
                idempotency_key=f"{idempotency_key}:embedded:{candidate.candidate_id}",
                generation_parameters={"stream_index": candidate.stream_index},
            )
        elif candidate.source_type == "sidecar":
            if candidate.asset_id is None:
                raise ValueError("sidecar candidate has no asset")
            original = self.session.get(Asset, candidate.asset_id)
            if original is None:
                raise ValueError("sidecar subtitle asset is missing")
            stored = self.assets.store(
                content=self.blob_store.read(original.storage_key),
                kind="subtitle",
                media_type=original.media_type,
                project_id=source_asset.project_id,
                parent_asset_ids=(source_asset.id, original.id),
                provider="user-upload",
                idempotency_key=f"{idempotency_key}:sidecar:{candidate.candidate_id}",
                metadata={"filename": candidate.file_name},
            )
        else:
            if self.provider is None:
                raise ValueError("provider candidate cannot be downloaded without a provider")
            self._status_for_run(row.run_id, "subtitle_downloading")
            download = await self.provider.download(
                candidate, idempotency_key=f"{idempotency_key}:download:{candidate.candidate_id}"
            )
            stored = self.assets.store(
                content=download.content,
                kind="subtitle",
                media_type=download.media_type,
                project_id=source_asset.project_id,
                parent_asset_ids=(source_asset.id,),
                provider=download.provider,
                provider_request_id=download.provider_request_id,
                idempotency_key=f"{idempotency_key}:provider:{candidate.candidate_id}",
                generation_parameters={
                    "provider_subtitle_id": candidate.provider_subtitle_id,
                    "provider_file_id": candidate.provider_file_id,
                },
                metadata={"filename": download.file_name},
            )
            row.provider_request_id = download.provider_request_id
        row.asset_id = stored.id
        row.status = "downloaded"
        self.session.commit()
        material = self.session.get(Asset, stored.id)
        if material is None:
            raise RuntimeError("stored subtitle asset is missing")
        return material

    def _persist_transcript(
        self,
        project: Project,
        source_video: SourceVideo,
        run: SubtitleRun,
        candidate: SubtitleCandidate,
        subtitle_asset: Asset,
        cues: list[SubtitleCue],
        quality: SubtitleQuality,
        duration: float,
        voiced: list[TimeInterval] | None,
        warnings: list[TranscriptionWarning],
        idempotency_key: str,
    ) -> SubtitleImportResult:
        parsed_quality = quality
        segments = [
            TranscriptSegment(
                sequence=cue.sequence,
                start_seconds=cue.start_seconds,
                end_seconds=min(cue.end_seconds, duration),
                text=cue.text,
                source_chunk_ids=[subtitle_asset.id],
                words=[],
            )
            for cue in cues
            if cue.start_seconds < duration and min(cue.end_seconds, duration) > cue.start_seconds
        ]
        if not segments:
            raise SubtitleUnavailableError("subtitle has no cues inside the source duration")
        text = " ".join(segment.text for segment in segments)
        coverage = _coverage(parsed_quality, duration, voiced)
        transcript_id = uuid4()
        canonical = CanonicalSubtitleTranscriptArtifact(
            project_id=project.id,
            subtitle_run_id=run.id,
            transcript_id=transcript_id,
            source_video_id=source_video.id,
            source_subtitle_asset_id=subtitle_asset.id,
            language=candidate.language,
            text=text,
            segments=segments,
            coverage=coverage,
            candidate=candidate.model_copy(update={"asset_id": subtitle_asset.id}),
            quality=parsed_quality,
            warnings=warnings,
        )
        transcript_asset = self.assets.store(
            content=(json.dumps(canonical.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
            kind="json",
            media_type="application/json",
            project_id=project.id,
            parent_asset_ids=(subtitle_asset.id,),
            provider=candidate.provider,
            idempotency_key=f"{idempotency_key}:canonical-subtitle-transcript",
            generation_parameters={
                "candidate_id": candidate.candidate_id,
                "quality": parsed_quality.model_dump(mode="json"),
            },
        )
        transcript = Transcript(
            id=transcript_id,
            project_id=project.id,
            run_id=None,
            subtitle_run_id=run.id,
            version=self.transcripts.next_version(project.id),
            language=candidate.language,
            text=text,
            transcript_asset_id=transcript_asset.id,
            duration_seconds=duration,
            coverage_score=coverage.ratio,
            warnings=[warning.model_dump(mode="json") for warning in warnings],
        )
        self.session.add(transcript)
        self.session.flush()
        speaker_hints = {cue.sequence: cue.speaker_hint for cue in cues if cue.speaker_hint}
        for segment in segments:
            self.session.add(
                TranscriptSegmentRecord(
                    transcript_id=transcript.id,
                    sequence=segment.sequence,
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    text=segment.text,
                    speaker_label=None,
                    confidence=None,
                    source_chunk_ids=[str(subtitle_asset.id)],
                    words=[],
                    provenance={
                        "subtitle_candidate_id": candidate.candidate_id,
                        "source_subtitle_asset_id": str(subtitle_asset.id),
                        "speaker_hint": speaker_hints.get(segment.sequence),
                    },
                )
            )
        run.status = "subtitle_imported"
        run.error_code = None
        self.repository.select(run, transcript)
        project.status = "transcribed"
        return SubtitleImportResult(
            project_id=project.id,
            subtitle_run_id=run.id,
            transcript_id=transcript.id,
            source_video_id=source_video.id,
            source_subtitle_asset_id=subtitle_asset.id,
            transcript_asset_id=transcript_asset.id,
            status="subtitle_imported",
            language=candidate.language,
            text=text,
            segments=segments,
            coverage=coverage,
            candidate=canonical.candidate,
            quality=parsed_quality,
            warnings=warnings,
        )

    def _checkpoint_candidates(self, run: SubtitleRun, candidates: list[SubtitleCandidate]) -> None:
        existing = {row.candidate_id: row for row in self.repository.candidates(run.id)}
        for sequence, candidate in enumerate(candidates):
            row = existing.get(candidate.candidate_id)
            if row is None:
                self.session.add(
                    SubtitleCandidateRecord(
                        run_id=run.id,
                        sequence=sequence,
                        candidate_id=candidate.candidate_id,
                        source_type=candidate.source_type,
                        provider=candidate.provider,
                        provider_subtitle_id=candidate.provider_subtitle_id,
                        provider_file_id=candidate.provider_file_id,
                        asset_id=candidate.asset_id,
                        stream_index=candidate.stream_index,
                        language=candidate.language,
                        subtitle_format=candidate.subtitle_format,
                        status="discovered",
                        provider_metadata=candidate.metadata,
                    )
                )
            elif row.sequence != sequence or row.provider != candidate.provider:
                raise ValueError("subtitle candidate discovery changed for an existing run")

    def _voiced_intervals(
        self, source_audio: Asset | None, workspace: Path
    ) -> list[TimeInterval] | None:
        if source_audio is None:
            return None
        path = workspace / "transcription-audio.wav"
        self.blob_store.copy_to(source_audio.storage_key, path)
        duration = probe_duration(path)
        silence = detect_silence_ranges(path, duration_seconds=duration)
        return voiced_intervals(duration, silence)

    def _result_from_asset(self, run: SubtitleRun, transcript: Transcript) -> SubtitleImportResult:
        asset = self.session.get(Asset, transcript.transcript_asset_id)
        if asset is None:
            raise ValueError("canonical subtitle transcript asset is missing")
        canonical = CanonicalSubtitleTranscriptArtifact.model_validate_json(
            self.blob_store.read(asset.storage_key)
        )
        return SubtitleImportResult(
            project_id=canonical.project_id,
            subtitle_run_id=run.id,
            transcript_id=transcript.id,
            source_video_id=canonical.source_video_id,
            source_subtitle_asset_id=canonical.source_subtitle_asset_id,
            transcript_asset_id=asset.id,
            status="subtitle_imported",
            language=canonical.language,
            text=canonical.text,
            segments=canonical.segments,
            coverage=canonical.coverage,
            candidate=canonical.candidate,
            quality=canonical.quality,
            warnings=canonical.warnings,
        )

    def _matches(
        self, run: SubtitleRun, source_video_id: UUID, parameters: dict[str, object]
    ) -> bool:
        return (
            run.source_video_id == source_video_id
            and run.parameters == parameters
            and run.provider == (self.provider.provider_name if self.provider else None)
        )

    def _status(self, project: Project, run: SubtitleRun, value: str) -> None:
        project.status = value
        run.status = value
        self.session.commit()

    def _status_for_run(self, run_id: UUID, value: str) -> None:
        run = self.session.get(SubtitleRun, run_id)
        if run is not None:
            project = self.session.get(Project, run.project_id)
            if project is not None:
                self._status(project, run, value)


def _deduplicate_candidates(candidates: list[SubtitleCandidate]) -> list[SubtitleCandidate]:
    return list({candidate.candidate_id: candidate for candidate in candidates}.values())


def _format(candidate: SubtitleCandidate, asset: Asset) -> str:
    return _format_from_name_or_media(candidate.file_name or "", asset.media_type)


def _format_from_name_or_media(filename: str, media_type: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"srt", "vtt", "ass", "ssa"}:
        return suffix
    mapping = {
        "application/x-subrip": "srt",
        "text/srt": "srt",
        "text/vtt": "vtt",
        "text/x-ssa": "ass",
        "text/x-ass": "ass",
    }
    if media_type in mapping:
        return mapping[media_type]
    raise ValueError(f"unsupported subtitle media type: {media_type}")


def _language_from_filename(filename: str) -> str | None:
    parts = Path(filename).stem.lower().split(".")
    for value in reversed(parts):
        if re.fullmatch(r"[a-z]{2,3}", value):
            return value
    return None


def _media_identity(filename: str) -> tuple[str, int | None, int | None]:
    stem = Path(filename).stem
    episode = re.search(r"(?i)\bS(\d{1,2})E(\d{1,3})\b", stem)
    season_number = int(episode.group(1)) if episode else None
    episode_number = int(episode.group(2)) if episode else None
    title = stem[: episode.start()] if episode else stem
    title = re.sub(r"\b(?:19|20)\d{2}\b.*$", "", title)
    title = re.sub(r"[._-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or stem, season_number, episode_number


def _coverage(
    quality: object, duration: float, voiced: list[TimeInterval] | None
) -> TranscriptCoverage:
    from vidgen.contracts.subtitles import SubtitleQuality

    parsed = SubtitleQuality.model_validate(quality)
    if voiced:
        voiced_seconds = sum(item.end_seconds - item.start_seconds for item in voiced)
        ratio = parsed.voiced_coverage or 0
    else:
        voiced_seconds = duration
        ratio = parsed.timeline_coverage
    return TranscriptCoverage(
        voiced_seconds=voiced_seconds,
        covered_voiced_seconds=voiced_seconds * ratio,
        ratio=ratio,
        passed=parsed.passed,
        uncovered_intervals=[],
    )
