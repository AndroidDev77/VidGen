"""Restartable T20 orchestration.

The pipeline binds one stable QA identity to one target asset and drives the
stages in order: authoritative selection, deterministic measurement, sampling,
frame analysis, the first-pass visual agent, score recomputation, bounded
adjudication and persistence.

Restart safety is the point. Every stage checkpoints, and a repeated identical
request reuses the samples, contact sheet, deterministic diagnostics, provider
results, adjudication, recomputed score, evidence and final outcome. It creates
no second provider request, no second T23 attempt, no second reservation, no
second ledger charge, and no duplicate asset or row.

T20 emits repair codes. It never submits a new image or video generation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.qa import PIPELINE_VERSION
from services.qa.adjudication import evaluate_triggers, resolve
from services.qa.continuity import build_expectation, summarize_state
from services.qa.contracts import (
    AuthoritativeInputSelector,
    AuthoritativeQAInputs,
    VisualQALineageError,
    canonical_hash,
)
from services.qa.deterministic import (
    FFMPEG,
    FFPROBE,
    detect_region,
    detect_text,
    evaluate,
    expects_stillness,
    face_track_continuity,
    frame_interval_us,
    measure,
    merge,
    style_descriptor,
    style_distance,
    tool_version,
    warning_timestamps,
)
from services.qa.evidence import build_contact_sheet, deterministic_id
from services.qa.identity import (
    ambiguous_expectations,
    build_character_expectations,
    missing_identity_evidence,
    required_character_count,
)
from services.qa.pricing import estimate_visual_qa_cost, to_microusd
from services.qa.rubric import (
    ADJUDICATION_POLICY_VERSION,
    DETERMINISTIC_CHECK_VERSION,
    DETERMINISTIC_THRESHOLDS,
    PROMPT_VERSION,
    RUBRIC,
    SAMPLING_CONFIGURATION,
    THRESHOLDS,
    DeterministicThresholds,
    SamplingConfiguration,
    rubric_material,
)
from services.qa.sampler import (
    DecodedSample,
    SamplingError,
    decode_samples,
    load_still,
    plan_video_samples,
)
from services.qa.scoring import ScoringOutcome, build_dimension_results, decide, recompute
from services.qa.visual_agent import (
    EvidenceFrame,
    ReferenceImage,
    VisualAgent,
    VisualAgentCall,
    validate_result,
)
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.visual_qa import (
    VisualQAAdjudication,
    VisualQAAttemptType,
    VisualQADeterministicMetric,
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQADimensionResult,
    VisualQAFailureCode,
    VisualQAOutcome,
    VisualQAProviderDimensionScore,
    VisualQAProviderRequest,
    VisualQAProviderResult,
    VisualQAReferenceDescriptor,
    VisualQARepairCode,
    VisualQARepairRecommendation,
    VisualQAResult,
    VisualQARoutingRecommendation,
    VisualQASample,
    VisualQASampleReference,
    VisualQASamplingManifest,
    VisualQATargetType,
)
from vidgen.db.continuity_models import character_identity_versions, location_identity_versions
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.db.models import Asset
from vidgen.db.visual_qa_models import VisualQAAttempt, VisualQAResultRecord, VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.review.events import EventPayloadTooLarge, ProjectEventService, SequenceContention
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

QA_OPERATION = "visual_qa"


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VisualQAOptions:
    """Everything a caller may configure without touching the versioned policy."""

    sampling: SamplingConfiguration = SAMPLING_CONFIGURATION
    thresholds: DeterministicThresholds = DETERMINISTIC_THRESHOLDS
    expected_width: int | None = None
    expected_height: int | None = None
    trace_context: dict[str, str] | None = None


class VisualQAPipeline:
    """Evaluate one shot's keyframe or canonical video against its intent."""

    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        agent: VisualAgent,
        *,
        adjudicator: VisualAgent | None = None,
        shot_workflow_identity_resolver: Callable[..., str],
        options: VisualQAOptions | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.agent = agent
        self.adjudicator = adjudicator
        self.options = options or VisualQAOptions()
        self.metrics = metrics or Metrics()
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)
        self.repository = VisualQARepository(session)
        self.selector = AuthoritativeInputSelector(
            session, shot_workflow_identity_resolver=shot_workflow_identity_resolver
        )
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.visual_qa")

    # --- public API -------------------------------------------------------
    async def evaluate_shot(
        self,
        *,
        project_id: UUID,
        shot_id: UUID,
        target_type: VisualQATargetType,
        idempotency_key: str,
    ) -> VisualQAResult:
        inputs = self.selector.select(project_id, shot_id, target_type)
        identity, material = self._identity(inputs)
        run = self.repository.run_by_identity(identity)
        if run is not None and self.repository.is_complete(run):
            # A completed identical request reuses everything, including cost.
            return self._projection(run, inputs)
        if run is None:
            conflicting = self.repository.run_by_key(project_id, idempotency_key, target_type)
            if conflicting is not None and conflicting.qa_identity != identity:
                raise VisualQALineageError(
                    VisualQAFailureCode.IDENTITY_CONFLICT,
                    "idempotency key already binds different material QA inputs",
                    reference_id=conflicting.id,
                )
            run = self._create_run(inputs, identity, material, idempotency_key)
        try:
            return await self._run(run, inputs)
        except VisualQALineageError:
            raise
        except BaseException as error:
            logger.exception(
                "visual QA pipeline failed for run %s: %s",
                run.id if run is not None else "unknown",
                error,
            )
            self.session.rollback()
            durable = self.session.get(VisualQARun, run.id)
            if durable is not None:
                durable.status = "visual_qa_failed"
                durable.error_code = type(error).__name__[:128]
                self.session.commit()
            raise

    # --- identity ---------------------------------------------------------
    def _identity(self, inputs: AuthoritativeQAInputs) -> tuple[str, dict[str, Any]]:
        """Bind every material input, reference version and policy version."""
        target = inputs.target()
        material: dict[str, Any] = {
            "project_id": str(inputs.project.id),
            "storyboard_run_id": str(inputs.storyboard.id),
            "storyboard_input_hash": inputs.storyboard.input_hash,
            "shot_id": str(inputs.shot_record.id),
            "canonical_shot_hash": target.canonical_shot_hash,
            "shot_workflow_identity": inputs.shot_workflow_identity,
            "target_type": target.target_type.value,
            "target_asset_id": str(target.target_asset_id),
            "target_asset_sha256": target.target_asset_sha256,
            "reference_asset_ids": [str(item.asset_id) for item in inputs.references],
            "reference_asset_hashes": [item.sha256 for item in inputs.references],
            "identity_version_ids": sorted(
                str(item.identity_version_id) for item in inputs.references
            ),
            "character_state_hashes": list(inputs.character_state_hashes),
            "location_state_hash": inputs.location_state_hash,
            "bundle_hash": inputs.bundle_hash,
            "first_pass_provider": self.agent.name,
            "first_pass_model": self.agent.model,
            "contract_version": "visual-qa/1.0",
            "pipeline_version": PIPELINE_VERSION,
            **rubric_material(),
        }
        return canonical_hash(material), material

    def _create_run(
        self,
        inputs: AuthoritativeQAInputs,
        identity: str,
        material: dict[str, Any],
        idempotency_key: str,
    ) -> VisualQARun:
        target = inputs.target()
        run = VisualQARun(
            project_id=inputs.project.id,
            storyboard_run_id=inputs.storyboard.id,
            shot_id=inputs.shot_record.id,
            shot_workflow_identity=inputs.shot_workflow_identity,
            target_type=target.target_type.value,
            target_asset_id=target.target_asset_id,
            target_asset_sha256=target.target_asset_sha256,
            qa_identity=identity,
            input_hash=canonical_hash(material),
            idempotency_key=idempotency_key,
            status="visual_qa_queued",
            importance=target.importance.value,
            rubric_version=RUBRIC.rubric_version,
            sampling_version=self.options.sampling.version,
            threshold_version=THRESHOLDS.threshold_version,
            deterministic_version=DETERMINISTIC_CHECK_VERSION,
            pipeline_version=PIPELINE_VERSION,
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.flush()
        self.session.commit()
        return run

    # --- orchestration ----------------------------------------------------
    async def _run(self, run: VisualQARun, inputs: AuthoritativeQAInputs) -> VisualQAResult:
        source = self._materialize(inputs.target_asset)
        try:
            run.status = "visual_qa_measuring"
            self.session.commit()
            report, decoded = self._measure_and_sample(run, inputs, source)
            manifest = self._manifest(run, inputs, report)
            if not report.usable:
                # Deterministic proof of corruption: no paid request is made.
                outcome = self._deterministic_outcome(inputs, report, manifest)
                provider_result = _deterministic_provider_result(run.qa_identity, report)
                attempt = self._record_local_attempt(run, provider_result)
                return self._finalize(run, inputs, report, manifest, outcome, attempt, None)
            run.status = "visual_qa_evaluating"
            self.session.commit()
            request = self._provider_request(
                run, inputs, report, manifest, VisualQAAttemptType.FIRST_PASS, attempt_number=1
            )
            first_pass, first_attempt = await self._evaluate(run, inputs, request, decoded, None)
            outcome = self._score(inputs, first_pass, report, manifest, review_reasons=())
            triggers = evaluate_triggers(
                first_pass,
                report,
                outcome,
                thresholds=THRESHOLDS,
                ambiguity_reasons=self._ambiguity_reasons(inputs),
                prior_outcome=self._prior_outcome(inputs, run),
            )
            adjudication = None
            attempt = first_attempt
            if triggers and self.adjudicator is not None and THRESHOLDS.max_adjudication_attempts:
                run.status = "visual_qa_adjudicating"
                self.session.commit()
                adjudicated_request = self._provider_request(
                    run,
                    inputs,
                    report,
                    manifest,
                    VisualQAAttemptType.ADJUDICATION,
                    # Fixed, not "next": the attempt number is part of the
                    # attempt identity, so deriving it from the row count would
                    # mint a new identity on every resume and buy a second paid
                    # adjudication. The policy allows one, so it is always one.
                    attempt_number=1,
                    disagreements=triggers.disagreements,
                )
                adjudicated, attempt = await self._evaluate(
                    run, inputs, adjudicated_request, decoded, first_pass
                )
                adjudicated_outcome = self._score(
                    inputs, adjudicated, report, manifest, review_reasons=()
                )
                adjudication, final_outcome, review_reasons = resolve(
                    adjudication_id=deterministic_id("adjudication", run.qa_identity),
                    triggers=triggers,
                    first_pass=first_pass,
                    adjudicator=adjudicated,
                    adjudicated_outcome=adjudicated_outcome,
                    thresholds=THRESHOLDS,
                    attempts_used=1,
                )
                outcome = self._score(
                    inputs, adjudicated, report, manifest, review_reasons=review_reasons
                )
                if final_outcome is VisualQAOutcome.REVIEW:
                    outcome = self._score(
                        inputs,
                        adjudicated,
                        report,
                        manifest,
                        review_reasons=review_reasons or triggers.reasons,
                    )
            elif triggers:
                outcome = self._score(
                    inputs, first_pass, report, manifest, review_reasons=triggers.reasons
                )
            return self._finalize(run, inputs, report, manifest, outcome, attempt, adjudication)
        finally:
            source.cleanup()

    # --- stages -----------------------------------------------------------
    def _materialize(self, asset: Asset) -> _MaterializedAsset:
        """Stream the target asset into bounded temporary storage."""
        return _MaterializedAsset(self.blob_store, asset)

    def _measure_and_sample(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        source: _MaterializedAsset,
    ) -> tuple[VisualQADeterministicReport, list[DecodedSample]]:
        target_type = VisualQATargetType(run.target_type)
        existing = self.repository.samples(run.id)
        measurement = measure(source.path, target_type)
        report = evaluate(
            measurement,
            target_type=target_type,
            expected_width=self.options.expected_width,
            expected_height=self.options.expected_height,
            expected_duration_us=inputs.shot_record.usable_duration_us
            if target_type is VisualQATargetType.VIDEO
            else None,
            expects_stillness=expects_stillness(
                inputs.shot.camera.movement, inputs.shot.action.subject_action
            ),
            thresholds=self.options.thresholds,
            ffmpeg_version=tool_version(FFMPEG),
            ffprobe_version=tool_version(FFPROBE),
        )
        if not report.usable:
            run.deterministic_report = report.model_dump(mode="json")
            self.session.commit()
            return report, []
        if target_type is VisualQATargetType.KEYFRAME:
            decoded = [load_still(source.path)]
        else:
            plan = plan_video_samples(
                inputs.shot,
                measured_duration_us=report.measured_duration_us or 0,
                configuration=self.options.sampling,
                motion=measurement.motion,
                frame_interval_us=frame_interval_us(measurement.frame_rate),
                warning_timestamps_us=warning_timestamps(report),
            )
            try:
                decoded = decode_samples(source.path, plan)
            except SamplingError as error:
                report = merge(
                    report,
                    [
                        VisualQADeterministicMetric(
                            code="frame_sampling",
                            outcome="hard_failure",
                            tool=FFMPEG,
                            diagnostic_code="sample_decode_failed",
                            repair_code=VisualQARepairCode.DECODE_FAILURE,
                            message=str(error),
                        )
                    ],
                )
                run.deterministic_report = report.model_dump(mode="json")
                self.session.commit()
                return report, []
        if existing:
            # A resumed run keeps its persisted samples; only the frame bytes are
            # re-derived, and they are byte-identical by construction.
            report = merge(report, self._analyze(decoded, inputs, report))
            run.deterministic_report = report.model_dump(mode="json")
            self.session.commit()
            return report, decoded
        report = merge(report, self._analyze(decoded, inputs, report))
        samples = self._persist_samples(run, inputs, decoded)
        self._persist_contact_sheet(run, inputs, samples, decoded)
        run.deterministic_report = report.model_dump(mode="json")
        self.session.commit()
        return report, decoded

    def _analyze(
        self,
        decoded: Sequence[DecodedSample],
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
    ) -> list[VisualQADeterministicMetric]:
        """Run the frame-level deterministic analyzers over the sampled frames."""
        thresholds = self.options.thresholds
        metrics: list[VisualQADeterministicMetric] = []
        observations = [detect_text(sample.content) for sample in decoded]
        worst_index = max(
            range(len(observations)), key=lambda index: observations[index].confidence
        )
        worst = observations[worst_index]
        text_detected = worst.confidence >= thresholds.ocr_confidence_warning
        metrics.append(
            VisualQADeterministicMetric(
                code="unintended_text",
                measurement=worst.confidence,
                threshold=thresholds.ocr_confidence_warning,
                outcome="hard_failure" if text_detected else "pass",
                evidence_timestamp_us=decoded[worst_index].actual_timestamp_us,
                tool="pillow",
                tool_version=_pillow_version(),
                diagnostic_code="unintended_readable_text" if text_detected else "no_text_detected",
                repair_code=VisualQARepairCode.UNINTENDED_TEXT if text_detected else None,
                message="a sampled frame contains readable text the storyboard does not request"
                if text_detected
                else "",
            )
        )
        if len(decoded) > 1:
            regions = [detect_region(sample.content) for sample in decoded]
            continuity = face_track_continuity(regions)
            below = continuity < thresholds.face_track_continuity_floor
            metrics.append(
                VisualQADeterministicMetric(
                    code="face_track_continuity",
                    measurement=continuity,
                    threshold=thresholds.face_track_continuity_floor,
                    outcome="warning" if below else "pass",
                    evidence_timestamp_us=decoded[0].actual_timestamp_us,
                    tool="pillow",
                    tool_version=_pillow_version(),
                    diagnostic_code="face_track_discontinuity" if below else "face_track_ok",
                    repair_code=VisualQARepairCode.FACE_BREAKAGE if below else None,
                )
            )
        style_reference = self._style_reference(inputs)
        if style_reference is not None:
            reference_descriptor = style_descriptor(style_reference)
            distances = [
                style_distance(reference_descriptor, style_descriptor(sample.content))
                for sample in decoded
            ]
            worst_style = max(range(len(distances)), key=lambda index: distances[index])
            drifted = distances[worst_style] > thresholds.style_distance_warning
            metrics.append(
                VisualQADeterministicMetric(
                    code="style_distance",
                    measurement=distances[worst_style],
                    threshold=thresholds.style_distance_warning,
                    outcome="warning" if drifted else "pass",
                    evidence_timestamp_us=decoded[worst_style].actual_timestamp_us,
                    tool="pillow",
                    tool_version=_pillow_version(),
                    diagnostic_code="style_drift" if drifted else "style_within_threshold",
                    repair_code=VisualQARepairCode.STYLE_DRIFT if drifted else None,
                )
            )
        else:
            metrics.append(
                VisualQADeterministicMetric(
                    code="style_distance",
                    outcome="not_applicable",
                    tool="pillow",
                    tool_version=_pillow_version(),
                    diagnostic_code="no_configured_style_representation",
                    message="no approved reference is configured as the style representation",
                )
            )
        if (
            report.target_type is VisualQATargetType.VIDEO
            and len(decoded) < thresholds.minimum_sample_count
        ):
            metrics.append(
                VisualQADeterministicMetric(
                    code="evidence_coverage",
                    measurement=float(len(decoded)),
                    threshold=float(thresholds.minimum_sample_count),
                    outcome="warning",
                    tool="vidgen",
                    tool_version=DETERMINISTIC_CHECK_VERSION,
                    diagnostic_code="missing_required_evidence_coverage",
                    repair_code=VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE,
                )
            )
        return metrics

    def _style_reference(self, inputs: AuthoritativeQAInputs) -> bytes | None:
        """The approved location reference used as the deterministic style anchor."""
        for item in inputs.references:
            if item.role in {"location_identity", "location_state"}:
                asset = self.session.get(Asset, item.asset_id)
                if asset is not None and self.blob_store.exists(asset.storage_key):
                    return self.blob_store.read(asset.storage_key)
        return None

    def _persist_samples(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        decoded: Sequence[DecodedSample],
    ) -> list[VisualQASample]:
        samples: list[VisualQASample] = []
        for sequence, sample in enumerate(decoded):
            stored = self.assets.store(
                content=sample.content,
                kind="visual_qa_sample_frame",
                media_type="image/png",
                project_id=run.project_id,
                parent_asset_ids=(run.target_asset_id,),
                idempotency_key=f"visual-qa-sample:{run.qa_identity}:{sequence}",
                generation_parameters=self._asset_provenance(run, inputs),
                metadata={
                    "sample_type": sample.planned.sample_type.value,
                    "requested_timestamp_us": sample.planned.requested_timestamp_us,
                    "actual_timestamp_us": sample.actual_timestamp_us,
                    "selection_reason": sample.planned.reason,
                    "frame_sha256": sample.sha256,
                },
            )
            samples.append(
                VisualQASample(
                    sample_id=deterministic_id("sample", run.qa_identity, sequence),
                    sequence=sequence,
                    sample_type=sample.planned.sample_type,
                    requested_timestamp_us=sample.planned.requested_timestamp_us,
                    actual_timestamp_us=sample.actual_timestamp_us,
                    shot_relative_timestamp_us=sample.actual_timestamp_us,
                    frame_asset_id=stored.id,
                    frame_sha256=sample.sha256,
                    source_asset_id=run.target_asset_id,
                    selection_reason=sample.planned.reason,
                    contact_sheet_position=sequence,
                    measurements={"width": float(sample.width), "height": float(sample.height)},
                )
            )
        self.repository.persist_samples(run.id, samples)
        return samples

    def _persist_contact_sheet(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        samples: Sequence[VisualQASample],
        decoded: Sequence[DecodedSample],
    ) -> None:
        sheet = build_contact_sheet(
            list(zip(samples, [item.content for item in decoded], strict=True)),
            columns=self.options.sampling.contact_sheet_columns,
        )
        if sheet is None:
            return
        stored = self.assets.store(
            content=sheet.content,
            kind="visual_qa_contact_sheet",
            media_type=sheet.media_type,
            project_id=run.project_id,
            parent_asset_ids=(run.target_asset_id,),
            idempotency_key=f"visual-qa-contact-sheet:{run.qa_identity}",
            generation_parameters=self._asset_provenance(run, inputs),
            metadata={
                "columns": sheet.columns,
                "rows": sheet.rows,
                "positions": {str(key): value for key, value in sheet.positions.items()},
            },
        )
        run.contact_sheet_asset_id = stored.id

    def _manifest(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
    ) -> VisualQASamplingManifest:
        rows = self.repository.samples(run.id)
        # The manifest carries the *exact* decoded timestamps: clamping them to a
        # container-reported duration destroyed the evidence timestamps the
        # findings are anchored to, stopped the frames pairing with their samples,
        # and could collapse two samples onto one timestamp. A container that
        # under-reports its duration widens the recorded extent instead.
        measured = report.measured_duration_us or 0
        duration = max([measured, *(row.actual_timestamp_us for row in rows)]) if rows else measured
        samples = [
            VisualQASample(
                sample_id=row.id,
                sequence=row.sequence,
                sample_type=row.sample_type,  # type: ignore[arg-type]
                requested_timestamp_us=row.requested_timestamp_us,
                actual_timestamp_us=row.actual_timestamp_us,
                shot_relative_timestamp_us=row.shot_relative_timestamp_us,
                frame_asset_id=row.frame_asset_id,
                frame_sha256=row.frame_sha256,
                source_asset_id=row.source_asset_id,
                selection_reason=row.selection_reason,
                contact_sheet_position=row.contact_sheet_position,
                measurements={
                    str(key): float(value) for key, value in (row.measurements or {}).items()
                },
            )
            for row in rows
        ]
        manifest = VisualQASamplingManifest(
            sampling_version=self.options.sampling.version,
            target_type=VisualQATargetType(run.target_type),
            source_asset_id=run.target_asset_id,
            measured_duration_us=duration,
            samples=samples,
            contact_sheet_asset_id=run.contact_sheet_asset_id,
            contact_sheet_columns=self.options.sampling.contact_sheet_columns,
        )
        if run.sampling_manifest_asset_id is None:
            stored = self.assets.store(
                content=json.dumps(
                    manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode(),
                kind="visual_qa_sampling_manifest",
                media_type="application/json",
                project_id=run.project_id,
                parent_asset_ids=(run.target_asset_id,),
                idempotency_key=f"visual-qa-manifest:{run.qa_identity}",
                generation_parameters=self._asset_provenance(run, inputs),
            )
            run.sampling_manifest_asset_id = stored.id
        return manifest

    # --- provider ---------------------------------------------------------
    def _provider_request(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
        manifest: VisualQASamplingManifest,
        attempt_type: VisualQAAttemptType,
        *,
        attempt_number: int,
        disagreements: Sequence[str] = (),
    ) -> VisualQAProviderRequest:
        shot = inputs.shot
        expectations = build_character_expectations(inputs, self._identity_rows(inputs))
        location_row = self._location_row(inputs)
        expectation = build_expectation(
            inputs, location_row, previous_passed_qa=self._previous_passed(inputs)
        )
        attempt_identity = canonical_hash(
            {
                "qa_identity": run.qa_identity,
                "attempt_type": attempt_type.value,
                "attempt_number": attempt_number,
                "prompt_version": PROMPT_VERSION,
            }
        )
        diagnostics = [
            f"{metric.code}={metric.outcome}:{metric.diagnostic_code}"
            for metric in report.metrics
            if metric.outcome in {"warning", "hard_failure"}
        ]
        diagnostics.extend(f"disagreement:{item}" for item in disagreements)
        return VisualQAProviderRequest(
            qa_attempt_identity=attempt_identity,
            attempt_number=attempt_number,
            attempt_type=attempt_type,
            project_id=run.project_id,
            storyboard_shot_id=run.shot_id,
            target_type=VisualQATargetType(run.target_type),
            storyboard_objective=shot.visual_objective,
            required_character_ids=list(shot.incoming_continuity.present_character_ids),
            required_character_count=required_character_count(shot),
            required_location_id=expectation.location.location_id,
            character_state_summaries={
                str(item.character_id): item.summary() for item in expectations
            },
            location_state_summary=expectation.location.summary(),
            required_action=shot.action.subject_action,
            secondary_action=shot.action.secondary_action,
            camera_framing=shot.camera.framing,
            camera_angle=shot.camera.angle,
            camera_movement=shot.camera.movement,
            composition_requirements=[
                f"framing={shot.camera.framing}",
                f"angle={shot.camera.angle}",
                f"movement={shot.camera.movement}/{shot.camera.movement_intensity}",
                f"staging={shot.action.staging_note}" if shot.action.staging_note else "",
            ][:16],
            required_props=list(shot.prop_references),
            incoming_continuity_summary=summarize_state(expectation.incoming),
            outgoing_continuity_summary=summarize_state(expectation.outgoing),
            samples=[
                VisualQASampleReference(
                    sample_id=sample.sample_id,
                    sequence=sample.sequence,
                    sample_type=sample.sample_type,
                    shot_relative_timestamp_us=sample.shot_relative_timestamp_us,
                    source_relative_timestamp_us=sample.actual_timestamp_us,
                    frame_sha256=sample.frame_sha256,
                )
                for sample in manifest.samples
            ],
            contact_sheet_asset_id=manifest.contact_sheet_asset_id,
            references=[
                VisualQAReferenceDescriptor(
                    asset_id=item.asset_id,
                    sha256=item.sha256,
                    role=item.role,  # type: ignore[arg-type]
                    entity_id=item.entity_id,
                    identity_version_id=item.identity_version_id,
                    label=item.label,
                )
                for item in inputs.references
            ],
            deterministic_summary=[item for item in diagnostics if item][:32],
            rubric_version=RUBRIC.rubric_version,
            threshold_version=THRESHOLDS.threshold_version,
            prompt_version=PROMPT_VERSION,
            trace_context=dict(self.options.trace_context or {}),
        )

    async def _evaluate(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        request: VisualQAProviderRequest,
        decoded: Sequence[DecodedSample],
        first_pass: VisualQAProviderResult | None,
    ) -> tuple[VisualQAProviderResult, VisualQAAttempt]:
        """Run one bounded provider evaluation with T23 accounting, or reuse one."""
        agent = self.agent if first_pass is None else (self.adjudicator or self.agent)
        existing = self.repository.attempt_by_identity(request.qa_attempt_identity)
        if existing is not None and existing.status == "succeeded" and existing.result_projection:
            return (
                VisualQAProviderResult.model_validate(existing.result_projection),
                existing,
            )
        estimated = estimate_visual_qa_cost(
            frames=len(request.samples), references=len(request.references)
        )
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=agent.name,
            model=agent.model,
            operation=QA_OPERATION,
            input_hash=request.qa_attempt_identity,
            idempotency_key=request.qa_attempt_identity,
            related_entity_id=run.id,
            attempt_number=request.attempt_number,
            estimated_cost=estimated,
        ) as provider_attempt:
            attempt = existing or VisualQAAttempt(
                qa_run_id=run.id,
                attempt_number=request.attempt_number,
                attempt_type=request.attempt_type.value,
                attempt_identity=request.qa_attempt_identity,
                provider_attempt_id=provider_attempt.row.id,
                provider=agent.name,
                model=agent.model,
                status="pre_call_checkpoint",
            )
            attempt.provider_attempt_id = provider_attempt.row.id
            self.session.add(attempt)
            self.session.flush()
            reservation_id = self._reserve(run, provider_attempt.row.id, estimated, request)
            self.session.commit()  # durable pre-call checkpoint
            call = VisualAgentCall(
                request=request,
                # Pair by decoded timestamp, never by position: a resumed run can
                # decode a different frame count, and a positional zip would
                # attach the wrong sample ID to a frame.
                frames=_evidence_frames(request.samples, decoded),
                references=tuple(self._reference_images(inputs)),
                first_pass=first_pass,
            )
            try:
                result = await agent.evaluate(call)
                result = validate_result(
                    result,
                    request,
                    known_sample_ids=[sample.sample_id for sample in request.samples],
                )
            except BaseException as error:
                failure = classify_failure(error)
                attempt.status = "failed"
                attempt.failure_class = failure.failure_class
                attempt.error_code = failure.error_code
                attempt.completed_at = datetime.now(UTC)
                self._reconcile(reservation_id, request, Decimal("0"), billable=False)
                self.session.commit()
                raise
            provider_attempt.set_result(
                provider_request_id=result.provider_request_id,
                usage=[{"unit": key, "quantity": value} for key, value in result.usage.items()],
                actual_cost=estimated,
                metadata=dict(result.redacted_metadata),
            )
            attempt.status = "succeeded"
            attempt.provider_request_id = result.provider_request_id
            attempt.result_projection = result.model_dump(mode="json")
            attempt.completed_at = datetime.now(UTC)
            self._reconcile(reservation_id, request, estimated)
            run.cost_microusd = (run.cost_microusd or 0) + to_microusd(estimated)
            self.session.commit()
        return result, attempt

    def _reserve(
        self,
        run: VisualQARun,
        provider_attempt_id: UUID,
        estimated: Decimal,
        request: VisualQAProviderRequest,
    ) -> UUID | None:
        budget = self.session.scalar(
            select(ProjectBudget.id).where(ProjectBudget.project_id == run.project_id)
        )
        if budget is None:
            return None
        reservation = self.costs.reserve(
            CostReservationRequest(
                project_id=run.project_id,
                provider_attempt_id=provider_attempt_id,
                idempotency_key=f"{request.qa_attempt_identity}:reservation",
                estimated_amount=estimated,
                currency="USD",
            )
        )
        if reservation.decision in {
            BudgetDecision.DENY_ENTITY_CAP,
            BudgetDecision.DENY_HARD_CAP,
            BudgetDecision.UNKNOWN_PRICE_REVIEW,
        }:
            raise BudgetExceededError(f"visual QA denied: {reservation.decision}")
        return reservation.reservation_id

    def _reconcile(
        self,
        reservation_id: UUID | None,
        request: VisualQAProviderRequest,
        actual: Decimal,
        *,
        billable: bool = True,
    ) -> None:
        if reservation_id is None:
            return
        self.costs.reconcile(
            reservation_id,
            f"{request.qa_attempt_identity}:reconciliation",
            actual,
            billable=billable,
        )

    def _reference_images(self, inputs: AuthoritativeQAInputs) -> list[ReferenceImage]:
        """Only this shot's approved references; never the whole project."""
        images: list[ReferenceImage] = []
        for item in inputs.references:
            asset = self.session.get(Asset, item.asset_id)
            if asset is None or not self.blob_store.exists(asset.storage_key):
                continue
            images.append(
                ReferenceImage(
                    asset_id=item.asset_id,
                    role=item.role,
                    content=self.blob_store.read(asset.storage_key),
                    media_type=asset.media_type,
                )
            )
        return images

    # --- scoring and persistence -----------------------------------------
    def _score(
        self,
        inputs: AuthoritativeQAInputs,
        provider: VisualQAProviderResult,
        report: VisualQADeterministicReport,
        manifest: VisualQASamplingManifest,
        *,
        review_reasons: Sequence[str],
    ) -> ScoringOutcome:
        dimensions = build_dimension_results(
            provider,
            report,
            rubric=RUBRIC,
            samples=manifest.samples,
            source_asset_id=manifest.source_asset_id,
        )
        score = recompute(
            dimensions, rubric=RUBRIC, thresholds=THRESHOLDS, importance=inputs.importance
        )
        return decide(score, report, provider, thresholds=THRESHOLDS, review_reasons=review_reasons)

    def _deterministic_outcome(
        self,
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
        manifest: VisualQASamplingManifest | None,
    ) -> ScoringOutcome:
        provider = _deterministic_provider_result("0" * 64, report)
        samples = manifest.samples if manifest is not None else []
        dimensions = build_dimension_results(
            provider,
            report,
            rubric=RUBRIC,
            samples=samples,
            source_asset_id=inputs.target_asset.id,
        )
        score = recompute(
            dimensions, rubric=RUBRIC, thresholds=THRESHOLDS, importance=inputs.importance
        )
        return decide(score, report, provider, thresholds=THRESHOLDS)

    def _record_local_attempt(
        self, run: VisualQARun, provider: VisualQAProviderResult
    ) -> VisualQAAttempt:
        """Record the deterministic evaluation as an attempt with no paid call."""
        identity = canonical_hash({"qa_identity": run.qa_identity, "attempt": "deterministic"})
        existing = self.repository.attempt_by_identity(identity)
        if existing is not None:
            return existing
        attempt = VisualQAAttempt(
            qa_run_id=run.id,
            attempt_number=1,
            attempt_type=VisualQAAttemptType.FIRST_PASS.value,
            attempt_identity=identity,
            provider_attempt_id=None,
            provider="deterministic",
            model=DETERMINISTIC_CHECK_VERSION,
            status="succeeded",
            result_projection=provider.model_dump(mode="json"),
            completed_at=datetime.now(UTC),
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def _finalize(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
        manifest: VisualQASamplingManifest,
        outcome: ScoringOutcome,
        attempt: VisualQAAttempt,
        adjudication: Any,
    ) -> VisualQAResult:
        existing = self.repository.result_for_attempt(attempt.id)
        record = existing or VisualQAResultRecord(
            id=deterministic_id("result", run.qa_identity, attempt.attempt_identity),
            qa_run_id=run.id,
            attempt_id=attempt.id,
            created_at=datetime.now(UTC),
        )
        record.outcome = outcome.outcome.value
        record.recomputed_score = outcome.score.total
        record.pass_threshold = outcome.score.pass_threshold
        record.dimension_results = [
            item.model_dump(mode="json") for item in outcome.score.dimensions
        ]
        record.hard_failure = outcome.hard_failure
        record.hard_failure_codes = list(outcome.hard_failure_codes)
        record.warning_codes = list(outcome.warning_codes)
        record.repair_codes = [code.value for code in outcome.repair_codes]
        record.repair_recommendation = outcome.recommendation.routing.value
        record.confidence = outcome.score.confidence
        record.adjudication = (
            adjudication.model_dump(mode="json") if adjudication is not None else None
        )
        if existing is None:
            self.session.add(record)
        self.session.flush()
        self.repository.persist_evidence(
            run.id,
            record.id,
            [
                (finding.finding_id, item)
                for dimension in outcome.score.dimensions
                for finding in dimension.findings
                for item in finding.evidence
            ],
        )
        self.repository.mark_canonical(run, record)
        run.final_outcome = outcome.outcome.value
        run.final_score = outcome.score.total
        run.pass_threshold = outcome.score.pass_threshold
        run.hard_failure = outcome.hard_failure
        run.repair_recommendation = outcome.recommendation.routing.value
        run.repair_codes = [code.value for code in outcome.repair_codes]
        run.warning_codes = list(outcome.warning_codes)
        run.status = "visual_qa_complete"
        if run.completed_at is None:
            run.completed_at = datetime.now(UTC)
        result = self._build_result(run, inputs, report, manifest, outcome, adjudication)
        if run.report_asset_id is None:
            _report_key = f"visual-qa-report:{run.qa_identity}"
            # A prior attempt may have committed the asset but not the run (if the
            # session was rolled back after the asset commit). Reuse it rather than
            # trying to store a new report under the same idempotency key with
            # potentially different content (non-deterministic completed_at).
            _prior = self.assets.assets.get_by_idempotency(run.project_id, _report_key)
            if _prior is not None:
                run.report_asset_id = _prior.id
            else:
                stored = self.assets.store(
                    content=json.dumps(
                        result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ).encode(),
                    kind="visual_qa_report",
                    media_type="application/json",
                    project_id=run.project_id,
                    parent_asset_ids=(run.target_asset_id,),
                    idempotency_key=_report_key,
                    generation_parameters=self._asset_provenance(run, inputs),
                )
                run.report_asset_id = stored.id
        self.session.commit()
        # The run is durable and canonical from here on. Observability must never
        # be able to undo it: an exception raised while emitting would otherwise
        # reach evaluate_shot's failure handler and relabel a committed PASS as
        # visual_qa_failed, blocking the render on a metrics problem.
        try:
            self._emit_event(run, outcome)
            self._emit_metrics(run, outcome)
        except Exception:
            logger.exception("visual QA observability failed after the result was committed")
        return result

    def _build_result(
        self,
        run: VisualQARun,
        inputs: AuthoritativeQAInputs,
        report: VisualQADeterministicReport,
        manifest: VisualQASamplingManifest,
        outcome: ScoringOutcome,
        adjudication: Any,
    ) -> VisualQAResult:
        review = self.repository.latest_human_review(run.id)
        return VisualQAResult(
            qa_run_id=run.id,
            qa_identity=run.qa_identity,
            input_hash=run.input_hash,
            target=inputs.target(),
            outcome=outcome.outcome,
            score=outcome.score,
            hard_failure=outcome.hard_failure,
            hard_failure_codes=list(outcome.hard_failure_codes),
            warning_codes=list(outcome.warning_codes),
            repair_codes=list(outcome.repair_codes),
            recommendation=outcome.recommendation,
            deterministic_report=report,
            sampling_manifest=manifest,
            adjudication=adjudication,
            human_review_decision=review.decision if review else None,  # type: ignore[arg-type]
            human_reviewer=review.reviewer_principal if review else None,
            first_pass_provider=self.agent.name,
            first_pass_model=self.agent.model,
            pipeline_version=PIPELINE_VERSION,
            cost_microusd=run.cost_microusd or 0,
            created_at=_aware(run.completed_at),
        )

    def _projection(self, run: VisualQARun, inputs: AuthoritativeQAInputs) -> VisualQAResult:
        """Rebuild a completed result from persisted rows without any new work."""
        record = self.repository.canonical_result(run.id)
        if record is None:  # pragma: no cover - guarded by is_complete
            raise VisualQALineageError(
                VisualQAFailureCode.IDENTITY_CONFLICT,
                "completed QA run has no canonical result",
                reference_id=run.id,
            )
        report = VisualQADeterministicReport.model_validate(run.deterministic_report)
        manifest = self._manifest(run, inputs, report)
        outcome = ScoringOutcome(
            score=recompute(
                [VisualQADimensionResult.model_validate(item) for item in record.dimension_results],
                rubric=RUBRIC,
                thresholds=THRESHOLDS,
                importance=inputs.importance,
            ),
            outcome=VisualQAOutcome(record.outcome),
            hard_failure_codes=tuple(record.hard_failure_codes),
            warning_codes=tuple(record.warning_codes),
            repair_codes=tuple(VisualQARepairCode(code) for code in record.repair_codes),
            recommendation=_recommendation(record),
            review_reasons=(),
        )
        adjudication = (
            VisualQAAdjudication.model_validate(record.adjudication)
            if record.adjudication
            else None
        )
        return self._build_result(run, inputs, report, manifest, outcome, adjudication)

    # --- helpers ----------------------------------------------------------
    def _identity_rows(self, inputs: AuthoritativeQAInputs) -> dict[UUID, dict[str, Any]]:
        version_ids = {item.identity_version_id for item in inputs.references}
        if not version_ids:
            return {}
        rows = (
            self.session.execute(
                select(character_identity_versions).where(
                    character_identity_versions.c.id.in_(list(version_ids))
                )
            )
            .mappings()
            .all()
        )
        return {row["id"]: dict(row) for row in rows}

    def _location_row(self, inputs: AuthoritativeQAInputs) -> dict[str, Any] | None:
        if inputs.location_identity_version_id is None:
            return None
        row = (
            self.session.execute(
                select(location_identity_versions).where(
                    location_identity_versions.c.id == inputs.location_identity_version_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def _previous_passed(self, inputs: AuthoritativeQAInputs) -> bool:
        if inputs.previous_shot_record is None:
            return False
        passed, _ = self.repository.gate(inputs.previous_shot_record.id, VisualQATargetType.VIDEO)
        return passed

    def _prior_outcome(
        self, inputs: AuthoritativeQAInputs, run: VisualQARun
    ) -> VisualQAOutcome | None:
        """The most recent completed QA result for the same target, if one exists."""
        history = self.repository.runs_for_shot(inputs.project.id, run.shot_id)
        for previous in reversed(history):
            if (
                previous.id != run.id
                and previous.target_type == run.target_type
                and previous.final_outcome
            ):
                return VisualQAOutcome(previous.final_outcome)
        return None

    def _ambiguity_reasons(self, inputs: AuthoritativeQAInputs) -> tuple[str, ...]:
        expectations = build_character_expectations(inputs, self._identity_rows(inputs))
        reasons = list(ambiguous_expectations(expectations))
        missing = missing_identity_evidence(inputs.shot, expectations)
        reasons.extend(
            f"required character {character_id} has no approved T19 reference asset"
            for character_id in missing
        )
        return tuple(reasons[:8])

    def _asset_provenance(self, run: VisualQARun, inputs: AuthoritativeQAInputs) -> dict[str, Any]:
        """Immutable provenance recorded on every asset T20 stores."""
        return {
            "project_id": str(run.project_id),
            "storyboard_run_id": str(run.storyboard_run_id),
            "shot_id": str(run.shot_id),
            "target_asset_id": str(run.target_asset_id),
            "target_asset_sha256": run.target_asset_sha256,
            "shot_workflow_identity": run.shot_workflow_identity,
            "reference_parent_asset_ids": [str(item.asset_id) for item in inputs.references],
            "sampler_version": run.sampling_version,
            "deterministic_check_version": run.deterministic_version,
            "rubric_version": run.rubric_version,
            "threshold_version": run.threshold_version,
            "adjudication_policy_version": ADJUDICATION_POLICY_VERSION,
            "first_pass_provider": self.agent.name,
            "first_pass_model": self.agent.model,
            "qa_identity": run.qa_identity,
            "input_hash": run.input_hash,
            "pipeline_version": PIPELINE_VERSION,
            "provenance": "t20-visual-qa",
        }

    def _emit_event(self, run: VisualQARun, outcome: ScoringOutcome) -> None:
        """Append one bounded project event so the review UI stream shows QA."""
        try:
            ProjectEventService(self.session).append(
                run.project_id,
                event_type=f"visual_qa_{run.target_type}_completed",
                status=outcome.outcome.value.lower(),
                payload={
                    "failure_code": outcome.hard_failure_codes[0]
                    if outcome.hard_failure_codes
                    else None,
                    "warning_code": outcome.warning_codes[0] if outcome.warning_codes else None,
                },
            )
            self.session.commit()
        except (EventPayloadTooLarge, SequenceContention):
            # The event stream is a convenience projection; losing one event
            # never invalidates the canonical QA result that already committed.
            self.session.rollback()

    def _emit_metrics(self, run: VisualQARun, outcome: ScoringOutcome) -> None:
        """Bounded metric dimensions only: no IDs, prompts or signed URLs."""
        self.metrics.shot_qa.labels(run.rubric_version).observe(outcome.score.total)
        for code in outcome.repair_codes[:4]:
            self.metrics.shot_retry.labels(code.value).inc(0)


class _MaterializedAsset:
    """One target asset streamed into bounded temporary storage."""

    def __init__(self, blob_store: BlobStore, asset: Asset) -> None:
        self._directory = TemporaryDirectory(prefix="vidgen-qa-target-")
        suffix = ".png" if asset.media_type.startswith("image/") else ".mp4"
        self.path = Path(self._directory.name) / f"target{suffix}"
        self.path.write_bytes(blob_store.read(asset.storage_key))

    def cleanup(self) -> None:
        self._directory.cleanup()


def _evidence_frames(
    samples: Sequence[VisualQASampleReference], decoded: Sequence[DecodedSample]
) -> tuple[EvidenceFrame, ...]:
    """Attach frame bytes to the samples they were actually decoded for."""
    by_timestamp = {frame.actual_timestamp_us: frame for frame in decoded}
    frames: list[EvidenceFrame] = []
    for sample in samples:
        frame = by_timestamp.get(sample.source_relative_timestamp_us)
        if frame is None:
            continue
        frames.append(
            EvidenceFrame(
                sample_id=sample.sample_id,
                sequence=sample.sequence,
                shot_relative_timestamp_us=sample.shot_relative_timestamp_us,
                content=frame.content,
            )
        )
    return tuple(frames)


def _aware(value: datetime | None) -> datetime:
    """SQLite returns naive datetimes; the contract requires an explicit UTC offset."""
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _deterministic_provider_result(
    qa_identity: str, report: VisualQADeterministicReport
) -> VisualQAProviderResult:
    """A synthetic zero-score evaluation used when no paid call may be made."""
    return VisualQAProviderResult(
        qa_attempt_identity=qa_identity if len(qa_identity) == 64 else "0" * 64,
        attempt_type=VisualQAAttemptType.FIRST_PASS,
        dimension_scores=[
            VisualQAProviderDimensionScore(
                dimension=dimension,
                raw_score=0.0,
                confidence=1.0,
                applicable=True,
                summary="deterministic validation proved the asset unusable",
            )
            for dimension in VisualQADimension
        ],
        overall_confidence=1.0,
        provider="deterministic",
        model=report.check_version,
    )


def _recommendation(record: VisualQAResultRecord) -> VisualQARepairRecommendation:
    return VisualQARepairRecommendation(
        routing=VisualQARoutingRecommendation(record.repair_recommendation),
        repair_codes=[VisualQARepairCode(code) for code in record.repair_codes],
        rationale="",
    )


def _pillow_version() -> str:
    from PIL import __version__

    return f"pillow {__version__}"
