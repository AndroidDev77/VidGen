"""Restartable T10 scene-map/global-reduce application service."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Never
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.analysis.canonicalize import canonicalize
from services.analysis.provider import EpisodeAnalysisProvider, GenerationContext
from services.analysis.validator import validate_episode_analysis, validate_scene_analysis
from vidgen.contracts.episode_analysis import (
    AnalysisValidationReport,
    EpisodeAnalysisResult,
    EpisodeSynthesisRequest,
    SceneAnalysisRequest,
    SceneAnalysisResult,
    SceneEvidenceExcerpt,
    SourceReference,
)
from vidgen.db.episode_analysis_models import (
    AnalysisBeatDependency,
    AnalysisRelationship,
    AnalysisStateEvent,
    EpisodeAnalysisRecord,
    EpisodeAnalysisRun,
    SceneAnalysisCheckpoint,
)
from vidgen.db.episode_analysis_repository import EpisodeAnalysisRepository
from vidgen.db.models import (
    Asset,
    Character,
    Location,
    Project,
    Scene,
    SourceVideo,
)
from vidgen.db.models import (
    PlotBeat as PlotBeatRecord,
)
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore

CONTRACT_VERSION = "1.0"
PROMPT_VERSION = "episode-analysis-v1"
CONFIG_VERSION = "episode-provider-v1"


class EpisodeAnalysisPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: EpisodeAnalysisProvider,
        *,
        concurrency: int = 4,
        max_attempts: int = 2,
    ) -> None:
        self.session, self.blob_store, self.provider = session, blob_store, provider
        self.concurrency, self.max_attempts = concurrency, max_attempts
        self.repository = EpisodeAnalysisRepository(session)
        self.configuration_version = getattr(provider, "configuration_version", CONFIG_VERSION)

    async def process(
        self, *, project_id: UUID, evidence_package_id: UUID, idempotency_key: str
    ) -> EpisodeAnalysisResult:
        project = self.session.get(Project, project_id)
        evidence = self.session.get(EvidencePackageRecord, evidence_package_id)
        if project is None:
            raise ValueError("selected evidence package does not exist")

        def reject(message: str) -> Never:
            project.status = "episode_analysis_failed"
            self.session.commit()
            raise ValueError(message)

        if evidence is None or evidence.project_id != project_id:
            reject("selected evidence package does not exist")
        if not evidence.selected or evidence.provenance.get("package_asset_id") is None:
            reject("evidence package is incomplete or unselected")
        package_asset = self.session.get(Asset, UUID(str(evidence.provenance["package_asset_id"])))
        if (
            package_asset is None
            or package_asset.project_id != project_id
            or not self.blob_store.exists(package_asset.storage_key)
        ):
            reject("evidence package asset is missing or stale")
        source = self.session.get(SourceVideo, evidence.source_video_id)
        if source is None or source.duration_seconds is None:
            reject("evidence source is stale or has no duration")
        rows = list(
            self.session.scalars(
                select(SceneEvidenceRecord)
                .where(SceneEvidenceRecord.evidence_package_id == evidence.id)
                .order_by(SceneEvidenceRecord.scene_sequence)
            )
        )
        if not rows or any(
            item.get("severity") == "error" for item in evidence.provenance.get("diagnostics", [])
        ):
            reject("evidence package is incomplete")
        existing = self.repository.run_by_key(project_id, idempotency_key)
        if existing and existing.evidence_package_id != evidence.id:
            raise ValueError("idempotency key belongs to different evidence")
        run = existing or EpisodeAnalysisRun(
            project_id=project_id,
            source_video_id=source.id,
            evidence_package_id=evidence.id,
            idempotency_key=idempotency_key,
            input_hash=evidence.input_hash,
            contract_version=CONTRACT_VERSION,
            prompt_version=PROMPT_VERSION,
            provider_configuration_version=self.configuration_version,
            provider=getattr(self.provider, "provider", type(self.provider).__name__),
            model=getattr(self.provider, "model", "configured"),
            status="episode_analysis_pending",
            attempt_count=0,
            selected=False,
        )
        if existing is None:
            self.session.add(run)
            self.session.commit()
        completed = self.repository.completed(run.id)
        if completed:
            return EpisodeAnalysisResult(
                analysis_run_id=run.id,
                episode_analysis_id=completed.id,
                analysis_asset_id=completed.canonical_analysis_asset_id,
                version=completed.version,
                validation_report=AnalysisValidationReport.model_validate(run.validation_report),
            )
        project.status = run.status = "episode_scene_mapping"
        self.session.commit()
        checkpoints = self.repository.checkpoints(run.id)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def map_scene(row: SceneEvidenceRecord) -> SceneAnalysisResult:
            checkpoint = checkpoints.get(row.id)
            if checkpoint and checkpoint.status == "succeeded" and checkpoint.provider_result:
                return SceneAnalysisResult.model_validate(checkpoint.provider_result["output"])
            references = _references(evidence, row)
            request = SceneAnalysisRequest(
                project_id=project_id,
                evidence_package_id=evidence.id,
                scene_id=row.id,
                sequence=row.scene_sequence + 1,
                source_start_ms=round(row.source_start_seconds * 1000),
                source_end_ms=round(row.source_end_seconds * 1000),
                input_hash=_hash(row.evidence),
                idempotency_key=f"{idempotency_key}:scene:{row.id}",
                contract_version=CONTRACT_VERSION,
                prompt_version=PROMPT_VERSION,
                provider_configuration_version=self.configuration_version,
                evidence_references=references,
                evidence_excerpts=_excerpts(row),
            )
            checkpoint = checkpoint or SceneAnalysisCheckpoint(
                analysis_run_id=run.id,
                source_scene_id=row.id,
                sequence=request.sequence,
                input_hash=request.input_hash,
                idempotency_key=request.idempotency_key,
                status="pending",
                attempt_count=0,
            )
            if checkpoint not in self.session:
                self.session.add(checkpoint)
            attempts: list[dict[str, object]] = []
            feedback: str | None = None
            for attempt in range(1, self.max_attempts + 1):
                attempted_request = request.model_copy(
                    update={
                        "idempotency_key": request.idempotency_key
                        if feedback is None
                        else f"{request.idempotency_key}:repair:{attempt}"
                    }
                )
                try:
                    async with semaphore:
                        result = await self.provider.analyze_scene(
                            attempted_request,
                            GenerationContext(
                                attempt_number=attempt, validation_errors_json=feedback
                            ),
                        )
                except Exception as error:
                    attempts.append(
                        {"attempt": attempt, "error": type(error).__name__, "message": str(error)}
                    )
                    checkpoint.attempt_count = attempt
                    checkpoint.status = "failed"
                    checkpoint.provider_result = {"attempts": attempts}
                    self.session.commit()
                    continue
                report = validate_scene_analysis(
                    result.output,
                    scene_id=row.id,
                    sequence=request.sequence,
                    start_ms=request.source_start_ms,
                    end_ms=request.source_end_ms,
                    valid_reference_ids={item.reference_id for item in references},
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "result": result.model_dump(mode="json"),
                        "validation": report.model_dump(mode="json"),
                    }
                )
                checkpoint.provider_request_id = result.metadata.provider_request_id
                checkpoint.provider_result = {
                    "output": result.output.model_dump(mode="json"),
                    "attempts": attempts,
                }
                checkpoint.attempt_count = attempt
                run.attempt_count = max(run.attempt_count, attempt)
                checkpoint.validation_report = report.model_dump(mode="json")
                checkpoint.status = "succeeded" if report.valid else "invalid"
                self.session.commit()
                if report.valid:
                    return result.output
                feedback = report.model_dump_json()
            raise RuntimeError("scene attempts exhausted")

        try:
            scenes = list(await asyncio.gather(*(map_scene(row) for row in rows)))
        except Exception:
            run.status = project.status = "episode_analysis_failed"
            run.error_code = "SCENE_ANALYSIS_FAILED"
            self.session.commit()
            raise
        project.status = run.status = "episode_global_reduction"
        self.session.commit()
        request = EpisodeSynthesisRequest(
            project_id=project_id,
            evidence_package_id=evidence.id,
            source_video_id=source.id,
            duration_ms=round(source.duration_seconds * 1000),
            input_hash=evidence.input_hash,
            idempotency_key=f"{idempotency_key}:reduce",
            contract_version=CONTRACT_VERSION,
            prompt_version=PROMPT_VERSION,
            provider_configuration_version=self.configuration_version,
            scene_result_ids=[item.scene_id for item in scenes],
            scene_results=scenes,
        )
        valid_refs = {ref.reference_id for row in rows for ref in _references(evidence, row)}
        feedback = None
        reduced = None
        for attempt in range(1, self.max_attempts + 1):
            attempted_request = request.model_copy(
                update={
                    "idempotency_key": request.idempotency_key
                    if feedback is None
                    else f"{request.idempotency_key}:repair:{attempt}"
                }
            )
            try:
                reduced = await self.provider.synthesize_episode(
                    attempted_request,
                    GenerationContext(attempt_number=attempt, validation_errors_json=feedback),
                )
            except Exception:
                if attempt < self.max_attempts:
                    continue
                run.status = project.status = "episode_analysis_failed"
                run.error_code = "EPISODE_REDUCTION_FAILED"
                self.session.commit()
                raise
            analysis = canonicalize(reduced.output)
            project.status = run.status = "episode_analysis_validating"
            self.session.commit()
            report = validate_episode_analysis(
                analysis,
                valid_scene_ids={row.id for row in rows},
                valid_reference_ids=valid_refs,
                required_anonymous_labels={
                    label for scene in scenes for label in scene.anonymous_speaker_references
                },
            )
            if report.valid:
                break
            feedback = report.model_dump_json()
        run.validation_report = report.model_dump(mode="json")
        if not report.valid:
            run.status = project.status = "episode_analysis_failed"
            run.error_code = "EPISODE_VALIDATION_FAILED"
            self.session.commit()
            raise ValueError("episode analysis failed deterministic validation")
        if reduced is None:
            raise RuntimeError("episode reduction produced no result")
        parent_ids = (package_asset.id, *(item.id for item in package_asset.parents))
        asset = AssetService(self.session, self.blob_store).store(
            content=analysis.model_dump_json().encode(),
            kind="json",
            media_type="application/vnd.vidgen.episode-analysis+json",
            project_id=project_id,
            parent_asset_ids=parent_ids,
            provider=reduced.metadata.provider,
            provider_request_id=reduced.metadata.provider_request_id,
            idempotency_key=f"{idempotency_key}:asset",
            generation_parameters={
                "evidence_package_id": str(evidence.id),
                "input_hash": evidence.input_hash,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
                "provider_configuration_version": self.configuration_version,
                "model": reduced.metadata.model,
                "provider_request_ids": [
                    checkpoint.provider_request_id
                    for checkpoint in self.repository.checkpoints(run.id).values()
                    if checkpoint.provider_request_id
                ]
                + [reduced.metadata.provider_request_id],
            },
            metadata={
                "source_video_id": str(source.id),
                "evidence_package_id": str(evidence.id),
                "provider_metadata": reduced.metadata.model_dump(mode="json"),
                "validation_report": report.model_dump(mode="json"),
            },
        )
        self.session.query(EpisodeAnalysisRecord).filter_by(
            project_id=project_id, selected=True
        ).update({"selected": False})
        record = EpisodeAnalysisRecord(
            project_id=project_id,
            analysis_run_id=run.id,
            version=self.repository.next_version(project_id),
            canonical_analysis_asset_id=asset.id,
            input_hash=evidence.input_hash,
            duration_ms=analysis.duration_ms,
            character_count=len(analysis.characters),
            location_count=len(analysis.locations),
            scene_count=len(analysis.scenes),
            plot_beat_count=len(analysis.plot_beats),
            selected=True,
            warnings=[item.model_dump(mode="json") for item in analysis.warnings],
        )
        self.session.add(record)
        self.session.flush()
        self._persist_normalized(project_id, record.id, analysis)
        run.selected = True
        run.status = project.status = "episode_analyzed"
        self.session.commit()
        return EpisodeAnalysisResult(
            analysis_run_id=run.id,
            episode_analysis_id=record.id,
            analysis_asset_id=asset.id,
            version=record.version,
            validation_report=report,
        )

    def _persist_normalized(self, project_id: UUID, analysis_id: UUID, analysis: object) -> None:
        from vidgen.contracts.episode_analysis import EpisodeAnalysis

        canonical = EpisodeAnalysis.model_validate(analysis)
        for character in canonical.characters:
            character_row = self.session.get(Character, character.character_id)
            if character_row is None:
                self.session.add(
                    Character(
                        id=character.character_id,
                        project_id=project_id,
                        canonical_name=character.canonical_name,
                        definition=character.model_dump(mode="json"),
                    )
                )
            elif character_row.project_id != project_id:
                raise ValueError("character ID belongs to another project")
        for location in canonical.locations:
            location_row = self.session.get(Location, location.location_id)
            if location_row is None:
                self.session.add(
                    Location(
                        id=location.location_id,
                        project_id=project_id,
                        canonical_name=location.canonical_name,
                        definition=location.model_dump(mode="json"),
                    )
                )
            elif location_row.project_id != project_id:
                raise ValueError("location ID belongs to another project")
        source_scenes = {
            row.sequence + 1: row
            for row in self.session.scalars(select(Scene).where(Scene.project_id == project_id))
        }
        for scene in canonical.scenes:
            scene_row = source_scenes.get(scene.sequence)
            if scene_row is None:
                raise ValueError("canonical scene has no source scene projection")
            scene_row.summary = scene.summary
            scene_row.analysis = scene.model_dump(mode="json")
            scene_row.location_id = scene.location_id
        for beat in canonical.plot_beats:
            beat_row = self.session.get(PlotBeatRecord, beat.plot_beat_id)
            if beat_row is None:
                self.session.add(
                    PlotBeatRecord(
                        id=beat.plot_beat_id,
                        project_id=project_id,
                        sequence=beat.sequence,
                        summary=beat.summary,
                        importance=beat.importance,
                        required_for_coherence=beat.mandatory,
                        scene_ids=[str(item) for item in beat.scene_ids],
                    )
                )
        self.session.add_all(
            AnalysisStateEvent(
                analysis_id=analysis_id,
                stable_id=item.state_event_id,
                entity_id=item.entity_id,
                scene_id=item.scene_id,
                sequence=item.sequence,
                confidence=item.confidence,
                contract=item.model_dump(mode="json"),
            )
            for item in canonical.state_events
        )
        self.session.add_all(
            AnalysisRelationship(
                analysis_id=analysis_id,
                stable_id=item.relationship_id,
                source_character_id=item.source_character_id,
                target_character_id=item.target_character_id,
                confidence=item.confidence,
                description=item.description,
                contract=item.model_dump(mode="json"),
            )
            for item in canonical.relationships
        )
        self.session.add_all(
            AnalysisBeatDependency(
                analysis_id=analysis_id,
                cause_beat_id=item.cause_beat_id,
                effect_beat_id=item.effect_beat_id,
                contract=item.model_dump(mode="json"),
            )
            for item in canonical.beat_dependencies
        )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _references(package: EvidencePackageRecord, row: SceneEvidenceRecord) -> list[SourceReference]:
    result = [
        SourceReference(
            reference_type="source_scene",
            reference_id=row.id,
            scene_id=row.id,
            start_ms=round(row.source_start_seconds * 1000),
            end_ms=round(row.source_end_seconds * 1000),
        )
    ]
    result.append(
        SourceReference(reference_type="project", reference_id=package.project_id, scene_id=row.id)
    )
    for value in row.frame_asset_ids:
        result.append(
            SourceReference(reference_type="frame", reference_id=UUID(value), scene_id=row.id)
        )
    for item in row.evidence.get("transcript_items", []):
        result.append(
            SourceReference(
                reference_type="transcript_segment",
                reference_id=UUID(str(item["source_asset_id"])),
                scene_id=row.id,
                start_ms=round(item["source_range"]["start_seconds"] * 1000),
                end_ms=round(item["source_range"]["end_seconds"] * 1000),
            )
        )
    if package.contact_sheet_asset_id:
        result.append(
            SourceReference(
                reference_type="contact_sheet",
                reference_id=package.contact_sheet_asset_id,
                scene_id=row.id,
            )
        )
    return result


def _excerpts(row: SceneEvidenceRecord) -> list[SceneEvidenceExcerpt]:
    result: list[SceneEvidenceExcerpt] = []
    for item in row.evidence.get("transcript_items", []):
        result.append(
            SceneEvidenceExcerpt(
                text=item["text"],
                speaker_label=item.get("speaker_label"),
                source_reference=SourceReference(
                    reference_type="transcript_segment",
                    reference_id=UUID(str(item["source_asset_id"])),
                    scene_id=row.id,
                    start_ms=round(item["source_range"]["start_seconds"] * 1000),
                    end_ms=round(item["source_range"]["end_seconds"] * 1000),
                ),
            )
        )
    return result
