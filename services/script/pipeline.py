"""Restartable T11 compression and comedy script pipeline application service."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Never
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.script.canonicalize import canonicalize_plan, canonicalize_script
from services.script.provider import GenerationContext, ScriptGenerationProvider
from services.script.rubric import approval_recommendation, default_rubric
from services.script.settings import (
    ScriptGenerationSettings,
    ScriptSettingsError,
    resolve_script_settings,
)
from services.script.validator import (
    build_beat_coverage,
    validate_compressed_plot_plan,
    validate_recap_script,
)
from vidgen.contracts.episode_analysis import EpisodeAnalysis
from vidgen.contracts.script import (
    ComedyEditRequest,
    ComedyRubricScores,
    ComedyWritingRequest,
    CompressedPlotPlan,
    PlotCompressionRequest,
    RecapScript,
    ScriptGenerationResult,
    ScriptValidationReport,
)
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord, EpisodeAnalysisRun
from vidgen.db.models import Asset, Project
from vidgen.db.script_models import (
    CompressedPlotPlanRecord,
    Script,
    ScriptEditRecord,
    ScriptGenerationRun,
    ScriptReview,
)
from vidgen.db.script_models import ScriptSegment as ScriptSegmentRow
from vidgen.db.script_repository import ScriptRepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore

CONTRACT_VERSION = "1.0"
PROMPT_VERSION = "comedy-script-v1"
CONFIG_VERSION = "script-provider-v1"


class ScriptGenerationPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: ScriptGenerationProvider,
        *,
        max_repair_attempts: int = 2,
        max_revision_passes: int = 2,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.max_repair_attempts = max_repair_attempts
        self.max_revision_passes = max_revision_passes
        self.repository = ScriptRepository(session)
        self.assets = AssetService(session, blob_store)
        self.configuration_version = getattr(provider, "configuration_version", CONFIG_VERSION)
        self.rubric = default_rubric()

    async def process(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        setting_overrides: Mapping[str, object] | None = None,
    ) -> ScriptGenerationResult:
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError("project does not exist")

        def reject(message: str) -> Never:
            project.status = "script_generation_failed"
            self.session.commit()
            raise ValueError(message)

        analysis_record = self.session.scalar(
            select(EpisodeAnalysisRecord).where(
                EpisodeAnalysisRecord.project_id == project_id, EpisodeAnalysisRecord.selected
            )
        )
        if analysis_record is None:
            reject("project has no selected T10 episode analysis")
        if analysis_record.project_id != project_id:
            reject("episode analysis belongs to a different project")
        analysis_run = self.session.get(EpisodeAnalysisRun, analysis_record.analysis_run_id)
        if analysis_run is None or analysis_run.status != "episode_analyzed":
            reject("selected episode analysis is not in a valid analyzed status")
        analysis_asset = self.session.get(Asset, analysis_record.canonical_analysis_asset_id)
        if analysis_asset is None or not self.blob_store.exists(analysis_asset.storage_key):
            reject("canonical episode analysis asset is missing")
        analysis = EpisodeAnalysis.model_validate_json(
            self.blob_store.read(analysis_asset.storage_key)
        )

        try:
            settings = resolve_script_settings(project, setting_overrides)
        except ScriptSettingsError as error:
            reject(str(error))

        beat_ids = {beat.plot_beat_id for beat in analysis.plot_beats}
        missing_required = sorted(str(b) for b in set(settings.required_beat_ids) - beat_ids)
        if missing_required:
            reject(f"required beat IDs do not resolve in the episode analysis: {missing_required}")

        input_hash = _settings_hash(analysis_record, settings)
        existing = self.repository.run_by_key(project_id, idempotency_key)
        if existing is not None and (
            existing.episode_analysis_id != analysis_record.id or existing.input_hash != input_hash
        ):
            raise ValueError(
                "idempotency key is bound to a different episode analysis or configuration; "
                "use a new idempotency key"
            )
        run = existing or ScriptGenerationRun(
            project_id=project_id,
            episode_analysis_id=analysis_record.id,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            status="plot_compression_pending",
            target_duration_ms=settings.target_duration_ms,
            target_word_count=settings.target_words,
            target_words_per_minute=settings.target_words_per_minute,
            humor_intensity=settings.humor_intensity,
            recap_mode=settings.recap_mode,
            provider_configuration_version=self.configuration_version,
            compressor_model=getattr(self.provider, "model", "configured"),
            writer_model=getattr(self.provider, "model", "configured"),
            editor_model=getattr(self.provider, "model", "configured"),
            compressor_prompt_version=PROMPT_VERSION,
            writer_prompt_version=PROMPT_VERSION,
            editor_prompt_version=PROMPT_VERSION,
            rubric_version=self.rubric.rubric_version,
            attempt_count=0,
            revision_count=0,
        )
        if existing is None:
            self.session.add(run)
            self.session.commit()

        selected_for_run = next(
            (item for item in self.repository.scripts_for_run(run.id) if item.selected), None
        )
        if selected_for_run is not None:
            return self._result(run, selected_for_run)
        if run.status == "script_review_required":
            return self._result(run, None)

        plan_record = self.repository.selected_plan(run.id)
        if plan_record is None:
            project.status = run.status = "compressing_plot"
            self.session.commit()
            plan_record = await self._compress(
                run, analysis, settings, analysis_asset, idempotency_key
            )
            if plan_record is None:
                run.status = project.status = "script_review_required"
                run.error_code = "COMPRESSION_VALIDATION_FAILED"
                self.session.commit()
                return self._result(run, None)
        plan = self._load_plan(plan_record)
        project.status = run.status = "plot_compressed"
        self.session.commit()

        scripts = self.repository.scripts_for_run(run.id)
        if not scripts:
            project.status = run.status = "comedy_writing"
            self.session.commit()
            draft_record = await self._write_draft(
                run, plan_record, plan, analysis, settings, idempotency_key
            )
            if draft_record is None:
                run.status = project.status = "script_review_required"
                run.error_code = "DRAFT_VALIDATION_FAILED"
                self.session.commit()
                return self._result(run, None)
            scripts = [draft_record]
        candidate_record = scripts[-1]
        candidate = self._load_script(candidate_record)
        project.status = run.status = "script_validating"
        self.session.commit()

        completed_evaluations = sum(len(self.repository.reviews(item.id)) for item in scripts)
        approved_record = await self._run_editorial_loop(
            run,
            plan_record,
            plan,
            candidate_record,
            candidate,
            analysis,
            settings,
            idempotency_key,
            starting_evaluation=completed_evaluations + 1,
        )
        if approved_record is not None:
            self.session.query(Script).filter_by(project_id=project_id, selected=True).update(
                {"selected": False}
            )
            approved_record.selected = True
            approved_record.status = "approved"
            run.status = project.status = "script_approved"
            self.session.commit()
            return self._result(run, approved_record)
        run.status = project.status = "script_review_required"
        run.error_code = "REVISION_EXHAUSTED"
        self.session.commit()
        return self._result(run, None)

    async def _compress(
        self,
        run: ScriptGenerationRun,
        analysis: EpisodeAnalysis,
        settings: ScriptGenerationSettings,
        analysis_asset: Asset,
        idempotency_key: str,
    ) -> CompressedPlotPlanRecord | None:
        plan_id = uuid4()
        request = PlotCompressionRequest(
            project_id=run.project_id,
            episode_analysis_id=run.episode_analysis_id,
            episode_analysis=analysis,
            input_hash=run.input_hash,
            idempotency_key=_derived_key(idempotency_key, "compress"),
            contract_version=CONTRACT_VERSION,
            prompt_version=PROMPT_VERSION,
            provider_configuration_version=self.configuration_version,
            target_duration_ms=settings.target_duration_ms,
            target_words=settings.target_words,
            target_words_per_minute=settings.target_words_per_minute,
            required_beat_ids=settings.required_beat_ids,
            excluded_topics=settings.excluded_topics,
            recap_mode=settings.recap_mode,
        )
        feedback: str | None = None
        plan: CompressedPlotPlan | None = None
        report: ScriptValidationReport | None = None
        result_metadata_request_id = None
        for attempt in range(1, self.max_repair_attempts + 1):
            attempted = request.model_copy(
                update={
                    "idempotency_key": request.idempotency_key
                    if feedback is None
                    else f"{request.idempotency_key}:repair:{attempt}"
                }
            )
            result = await self.provider.compress_plot(
                attempted,
                GenerationContext(attempt_number=attempt, validation_errors_json=feedback),
            )
            plan = canonicalize_plan(result.output.model_copy(update={"plan_id": plan_id}))
            report = validate_compressed_plot_plan(plan, analysis=analysis, request=request)
            run.attempt_count = max(run.attempt_count, attempt)
            result_metadata_request_id = result.metadata.provider_request_id
            if report.valid:
                break
            feedback = report.model_dump_json()
        self.session.commit()
        if plan is None or report is None or not report.valid:
            return None
        asset = self.assets.store(
            content=plan.model_dump_json().encode(),
            kind="json",
            media_type="application/vnd.vidgen.compressed-plot-plan+json",
            project_id=run.project_id,
            parent_asset_ids=(analysis_asset.id,),
            provider=getattr(self.provider, "provider", type(self.provider).__name__),
            provider_request_id=result_metadata_request_id,
            idempotency_key=_derived_key(idempotency_key, "compress:asset"),
            generation_parameters={
                "generation_run_id": str(run.id),
                "input_hash": run.input_hash,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
                "provider_configuration_version": self.configuration_version,
            },
            metadata={"validation_report": report.model_dump(mode="json")},
        )
        record = CompressedPlotPlanRecord(
            project_id=run.project_id,
            generation_run_id=run.id,
            episode_analysis_id=run.episode_analysis_id,
            version=self.repository.next_plan_version(run.id),
            input_hash=run.input_hash,
            canonical_plan_asset_id=asset.id,
            selected_beat_count=len(plan.selected_beats),
            omitted_beat_count=len(plan.omitted_beats),
            target_word_count=settings.target_words,
            validation_report=report.model_dump(mode="json"),
            selected=True,
        )
        self.session.add(record)
        self.session.commit()
        return record

    async def _write_draft(
        self,
        run: ScriptGenerationRun,
        plan_record: CompressedPlotPlanRecord,
        plan: CompressedPlotPlan,
        analysis: EpisodeAnalysis,
        settings: ScriptGenerationSettings,
        idempotency_key: str,
    ) -> Script | None:
        script_id = uuid4()
        request = ComedyWritingRequest(
            project_id=run.project_id,
            episode_analysis_id=run.episode_analysis_id,
            compressed_plot_plan_id=plan_record.id,
            input_hash=run.input_hash,
            idempotency_key=_derived_key(idempotency_key, "write"),
            contract_version=CONTRACT_VERSION,
            prompt_version=PROMPT_VERSION,
            provider_configuration_version=self.configuration_version,
            compressed_plot=plan,
            channel_voice=settings.channel_voice,
            humor_intensity=settings.humor_intensity,
            prohibited_patterns=settings.prohibited_patterns,
            target_words=settings.target_words,
            recap_mode=settings.recap_mode,
        )
        feedback: str | None = None
        script: RecapScript | None = None
        report: ScriptValidationReport | None = None
        provider_request_id: str | None = None
        for attempt in range(1, self.max_repair_attempts + 1):
            attempted = request.model_copy(
                update={
                    "idempotency_key": request.idempotency_key
                    if feedback is None
                    else f"{request.idempotency_key}:repair:{attempt}"
                }
            )
            result = await self.provider.write_script(
                attempted,
                GenerationContext(attempt_number=attempt, validation_errors_json=feedback),
            )
            candidate = result.output.model_copy(update={"script_id": script_id, "version": 1})
            coverage = build_beat_coverage(candidate, plan)
            candidate = canonicalize_script(
                candidate.model_copy(update={"beat_coverage": coverage})
            )
            report = validate_recap_script(
                candidate,
                analysis=analysis,
                plan=plan,
                prohibited_patterns=settings.prohibited_patterns,
            )
            run.attempt_count = max(run.attempt_count, attempt)
            provider_request_id = result.metadata.provider_request_id
            script = candidate
            if report.valid:
                break
            feedback = report.model_dump_json()
        self.session.commit()
        if script is None or report is None or not report.valid:
            return None
        draft_record = self._persist_script_version(
            run,
            plan_record,
            script,
            parent=None,
            provider_request_id=provider_request_id,
            validation_report=report,
            idempotency_suffix="write:asset",
            idempotency_key=idempotency_key,
        )
        self.session.commit()
        return draft_record

    async def _run_editorial_loop(
        self,
        run: ScriptGenerationRun,
        plan_record: CompressedPlotPlanRecord,
        plan: CompressedPlotPlan,
        candidate_record: Script,
        candidate: RecapScript,
        analysis: EpisodeAnalysis,
        settings: ScriptGenerationSettings,
        idempotency_key: str,
        *,
        starting_evaluation: int = 1,
    ) -> Script | None:
        max_evaluations = self.max_revision_passes + 1
        evaluation = starting_evaluation
        while True:
            review_row = self._existing_review(candidate_record.id)
            next_version_row = self.session.scalar(
                select(Script).where(Script.parent_script_id == candidate_record.id)
            )
            if review_row is not None and next_version_row is not None:
                recommendation = review_row.approval_recommendation
                revised_record = next_version_row
                revised = self._load_script(revised_record)
            else:
                project = self.session.get(Project, run.project_id)
                assert project is not None
                project.status = run.status = "comedy_editing"
                self.session.commit()
                request = ComedyEditRequest(
                    project_id=run.project_id,
                    script_id=candidate_record.id,
                    script_version=candidate_record.version,
                    recap_script=candidate,
                    compressed_plot=plan,
                    rubric=self.rubric,
                    attempt_number=evaluation,
                    input_hash=run.input_hash,
                    idempotency_key=_derived_key(
                        idempotency_key, "edit", f"{candidate_record.id}:{evaluation}"
                    ),
                    contract_version=CONTRACT_VERSION,
                    prompt_version=PROMPT_VERSION,
                    rubric_version=self.rubric.rubric_version,
                    provider_configuration_version=self.configuration_version,
                )
                result = await self.provider.edit_script(
                    request, GenerationContext(attempt_number=evaluation)
                )
                revised = result.output.revised_script.model_copy(
                    update={
                        "script_id": uuid4(),
                        "version": candidate_record.version,
                        "parent_script_id": candidate_record.id,
                    }
                )
                coverage = build_beat_coverage(revised, plan)
                revised = canonicalize_script(
                    revised.model_copy(update={"beat_coverage": coverage})
                )
                previous_coverage = {
                    item.plot_beat_id: item.coverage for item in candidate.beat_coverage
                }
                report = validate_recap_script(
                    revised,
                    analysis=analysis,
                    plan=plan,
                    prohibited_patterns=settings.prohibited_patterns,
                    previous_script=candidate,
                    previous_coverage=previous_coverage,
                )
                mandatory_total = sum(1 for item in coverage if item.mandatory)
                mandatory_covered = sum(
                    1 for item in coverage if item.mandatory and item.coverage == "covered"
                )
                mandatory_ratio = (
                    1.0 if mandatory_total == 0 else mandatory_covered / mandatory_total
                )
                within_target = (
                    revised.target_word_count == 0
                    or abs(revised.actual_word_count - revised.target_word_count)
                    / revised.target_word_count
                    <= 0.05
                )
                recommendation = approval_recommendation(
                    result.output.scores,
                    self.rubric,
                    mandatory_coverage_ratio=mandatory_ratio,
                    word_count_within_target=within_target,
                    validation_valid=report.valid,
                )
                revised_record = self._persist_script_version(
                    run,
                    plan_record,
                    revised,
                    parent=candidate_record,
                    provider_request_id=result.metadata.provider_request_id,
                    validation_report=report,
                    idempotency_suffix=f"edit:{evaluation}:asset",
                    idempotency_key=idempotency_key,
                    review_scores=result.output.scores.model_dump(mode="json"),
                )
                review_row = ScriptReview(
                    script_id=candidate_record.id,
                    review_sequence=self.repository.next_review_sequence(candidate_record.id),
                    provider_request_id=result.metadata.provider_request_id,
                    attempt_number=evaluation,
                    rubric_version=self.rubric.rubric_version,
                    scores=result.output.scores.model_dump(mode="json"),
                    issues=[item.model_dump(mode="json") for item in result.output.issues],
                    approval_recommendation=recommendation,
                    validation_report=report.model_dump(mode="json"),
                )
                self.session.add(review_row)
                self.session.flush()
                self.session.add_all(
                    ScriptEditRecord(
                        review_id=review_row.id,
                        segment_id=edit.segment_id,
                        old_content_hash=_hash(edit.old_text),
                        new_content_hash=_hash(edit.new_text),
                        old_text=edit.old_text,
                        new_text=edit.new_text,
                        reason=edit.reason,
                        rubric_dimensions=list(edit.rubric_dimensions),
                        applied=True,
                    )
                    for edit in result.output.edits
                )
                if evaluation > 1:
                    run.revision_count += 1
                self.session.commit()
            if recommendation == "approve":
                return revised_record
            if evaluation >= max_evaluations:
                return None
            candidate_record, candidate = revised_record, revised
            evaluation += 1

    def _existing_review(self, script_id: UUID) -> ScriptReview | None:
        return self.session.scalar(
            select(ScriptReview)
            .where(ScriptReview.script_id == script_id)
            .order_by(ScriptReview.review_sequence)
        )

    def _persist_script_version(
        self,
        run: ScriptGenerationRun,
        plan_record: CompressedPlotPlanRecord,
        script: RecapScript,
        *,
        parent: Script | None,
        provider_request_id: str | None,
        validation_report: ScriptValidationReport,
        idempotency_suffix: str,
        idempotency_key: str,
        review_scores: dict[str, object] | None = None,
    ) -> Script:
        version = self.repository.next_script_version(run.project_id)
        script = script.model_copy(update={"version": version})
        asset = self.assets.store(
            content=script.model_dump_json().encode(),
            kind="json",
            media_type="application/vnd.vidgen.recap-script+json",
            project_id=run.project_id,
            parent_asset_ids=(plan_record.canonical_plan_asset_id,),
            provider=getattr(self.provider, "provider", type(self.provider).__name__),
            provider_request_id=provider_request_id,
            idempotency_key=_derived_key(idempotency_key, idempotency_suffix),
            generation_parameters={
                "generation_run_id": str(run.id),
                "compressed_plot_plan_id": str(plan_record.id),
                "parent_script_asset_id": str(parent.canonical_script_asset_id) if parent else None,
                "input_hash": run.input_hash,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
                "provider_configuration_version": self.configuration_version,
            },
            metadata={"validation_report": validation_report.model_dump(mode="json")},
        )
        record = Script(
            id=script.script_id,
            project_id=run.project_id,
            generation_run_id=run.id,
            episode_analysis_id=run.episode_analysis_id,
            compressed_plot_plan_id=plan_record.id,
            parent_script_id=parent.id if parent else None,
            version=version,
            status="draft" if parent is None else "revised",
            target_word_count=script.target_word_count,
            actual_word_count=script.actual_word_count,
            target_duration_ms=script.target_duration_ms,
            humor_intensity=script.humor_intensity,
            canonical_script_asset_id=asset.id,
            prompt_version=PROMPT_VERSION,
            rubric_version=self.rubric.rubric_version if review_scores else None,
            review_scores=review_scores,
            selected=False,
        )
        self.session.add(record)
        self.session.flush()
        self.session.add_all(
            ScriptSegmentRow(
                stable_segment_id=segment.segment_id,
                script_id=record.id,
                sequence=segment.sequence,
                segment_type=segment.type,
                speaker_kind=segment.speaker_kind,
                speaker_character_id=segment.speaker_character_id,
                anonymous_speaker_label=segment.anonymous_speaker_label,
                text=segment.text,
                content_hash=segment.content_hash,
                plot_beat_ids=[str(item) for item in segment.plot_beat_ids],
                source_scene_ids=[str(item) for item in segment.source_scene_ids],
                joke_annotations=[
                    item.model_dump(mode="json") for item in segment.joke_annotations
                ],
                visual_gag=segment.visual_gag,
                estimated_duration_ms=segment.estimated_duration_ms,
                voice_direction=segment.voice_direction,
                locked=segment.locked,
            )
            for segment in script.segments
        )
        # Flush only: callers that persist a review/edit trail alongside this
        # version (the editorial loop) must commit both together, or a crash
        # between two separate commits leaves an orphaned child script with no
        # review row — on resume, restart detection (`_existing_review`) then
        # can't find it and calls the provider again, creating a second child
        # of the same parent and making the "next version" ambiguous.
        self.session.flush()
        return record

    def _load_plan(self, record: CompressedPlotPlanRecord) -> CompressedPlotPlan:
        asset = self.session.get(Asset, record.canonical_plan_asset_id)
        assert asset is not None
        return CompressedPlotPlan.model_validate_json(self.blob_store.read(asset.storage_key))

    def _load_script(self, record: Script) -> RecapScript:
        asset = self.session.get(Asset, record.canonical_script_asset_id)
        assert asset is not None
        return RecapScript.model_validate_json(self.blob_store.read(asset.storage_key))

    def _result(
        self, run: ScriptGenerationRun, script_record: Script | None
    ) -> ScriptGenerationResult:
        plan_record = self.repository.selected_plan(run.id)
        review_scores = None
        if script_record is not None and script_record.review_scores:
            review_scores = ComedyRubricScores.model_validate(script_record.review_scores)
        return ScriptGenerationResult(
            generation_run_id=run.id,
            compressed_plot_plan_id=plan_record.id if plan_record else None,
            script_id=script_record.id if script_record else None,
            script_version=script_record.version if script_record else None,
            status=run.status,
            review_scores=review_scores,
            revision_count=run.revision_count,
        )


def _hash(value: object) -> str:
    payload = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _settings_hash(
    analysis_record: EpisodeAnalysisRecord, settings: ScriptGenerationSettings
) -> str:
    payload = {
        "analysis_id": str(analysis_record.id),
        "analysis_input_hash": analysis_record.input_hash,
        "analysis_version": analysis_record.version,
        "target_duration_ms": settings.target_duration_ms,
        "target_words": settings.target_words,
        "target_words_per_minute": settings.target_words_per_minute,
        "humor_intensity": settings.humor_intensity,
        "recap_mode": settings.recap_mode,
        "required_beat_ids": sorted(str(item) for item in settings.required_beat_ids),
        "excluded_topics": sorted(settings.excluded_topics),
        "channel_voice": settings.channel_voice.model_dump(mode="json"),
        "prohibited_patterns": sorted(settings.prohibited_patterns),
    }
    return _hash(payload)


def _derived_key(base: str, stage: str, extra: str = "") -> str:
    digest = hashlib.sha256(f"{base}:{stage}:{extra}".encode()).hexdigest()
    return f"script-{stage}:{digest}"
