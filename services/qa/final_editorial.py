"""Restartable T22 final editorial-QA orchestration.

The pipeline binds one stable final-QA identity to one T17 render identity and
drives the phases in order:

``INPUT_VALIDATION`` → ``DETERMINISTIC_MEDIA_QA`` → ``CAPTION_QA``
→ ``EDITORIAL_ANALYSIS`` → ``ADJUDICATION`` (when necessary) → ``COMPLETION_GATE``

Restart safety is the point. Every phase checkpoints, and a repeated identical
request reuses the validated inputs, the deterministic measurements, the caption
results, the provider result, the adjudication, the report and the gate. It
creates no second provider request, no second T23 attempt, no second
reservation, no second ledger charge, and no duplicate asset or row.

Two rules are enforced structurally rather than by convention:

* No paid request happens until deterministic media, audio and caption checks
  permit it. A render that will not decode is never analysed for story.
* The gate is recomputed from validated findings. ``PASS`` is earned; it is
  never a default and never something a human can assert over a measurement.

T22 identifies and gates. It never makes a paid generation call and never starts
another creative repair loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.qa import final_audio, final_captions, final_deterministic
from services.qa.final_editorial_provider import (
    EditorialFrame,
    FinalEditorialCall,
    FinalEditorialProvider,
    validate_result,
)
from services.qa.final_evidence import (
    SampledFrame,
    build_contact_sheet,
    deterministic_id,
    extract_frames,
    frame_evidence,
)
from services.qa.final_gate import (
    adjudication_triggers,
    apply_adjudication,
    bound_findings,
    decide,
    findings_from_checks,
    findings_from_provider,
    remediation_routes,
)
from services.qa.final_inputs import (
    AuthoritativeFinalInputs,
    FinalInputSelector,
    FinalQALineageError,
)
from services.qa.final_rubric import (
    ADJUDICATION_POLICY_VERSION,
    DEFAULT_CONFIGURATION,
    EDITORIAL_DIMENSIONS,
    FINAL_QA_PIPELINE_VERSION,
    GATE_VERSION,
    canonical_hash,
    configuration_hash,
    rubric_material,
)
from services.qa.pricing import to_microusd
from services.renderer.captions import caption_identity as caption_track_identity
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.final_editorial import (
    ADJUDICATION_CONFIDENCE_FLOOR,
    FinalCaptionCheck,
    FinalDeterministicCheck,
    FinalEditorialAdjudication,
    FinalEditorialDimension,
    FinalEditorialFinding,
    FinalEditorialProviderRequest,
    FinalEditorialProviderResult,
    FinalEditorialReport,
    FinalEditorialResult,
    FinalQAConfiguration,
    FinalQADecision,
    FinalQAFailureCode,
    FinalQAInput,
    FinalQAPhase,
    FinalQAStatus,
    FinalRemediationTarget,
)
from vidgen.contracts.render import CaptionCue, CaptionTrack
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.db.final_editorial_models import FinalEditorialProviderAttempt, FinalEditorialRun
from vidgen.db.final_editorial_repository import FinalEditorialRepository
from vidgen.db.models import Asset
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

FINAL_QA_OPERATION = "final_editorial_qa"

#: Deterministic, conservative estimate used to reserve budget before the call.
#: Actual usage is reconciled from the provider result.
BASE_CALL_COST = Decimal("0.05")
COST_PER_FRAME = Decimal("0.004")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FinalQAOptions:
    """Everything a caller may configure without touching the versioned policy."""

    configuration: FinalQAConfiguration = field(default_factory=lambda: DEFAULT_CONFIGURATION)
    trace_context: dict[str, str] | None = None
    adjudicate: bool = True


class FinalEditorialPipeline:
    """Evaluate one project's assembled recap and gate its completion."""

    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: FinalEditorialProvider,
        *,
        adjudicator: FinalEditorialProvider | None = None,
        options: FinalQAOptions | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.adjudicator = adjudicator
        self.options = options or FinalQAOptions()
        self.metrics = metrics or Metrics()
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)
        self.repository = FinalEditorialRepository(session)
        self.selector = FinalInputSelector(session, blob_store)
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.final_editorial")

    # --- public API -------------------------------------------------------
    async def evaluate_project(
        self, *, project_id: UUID, idempotency_key: str
    ) -> FinalEditorialResult:
        selected = self.selector.select(project_id)
        identity, material = self._identity(selected.inputs)
        run = self.repository.run_by_identity(identity)
        if run is not None and self.repository.is_complete(run):
            # A completed identical request reuses everything, including cost.
            return self._projection(run, reused=True)
        if run is None:
            conflicting = self.repository.run_by_key(project_id, idempotency_key)
            if conflicting is not None and conflicting.final_qa_identity != identity:
                raise FinalQALineageError(
                    FinalQAFailureCode.IDENTITY_CONFLICT,
                    "idempotency key already binds different material final-QA inputs",
                    reference_id=conflicting.id,
                )
            run = self._create_run(selected, identity, material, idempotency_key)
        try:
            return await self._run(run, selected)
        except FinalQALineageError as error:
            self.repository.checkpoint(
                run,
                status=FinalQAStatus.FINAL_QA_FAILED,
                phase=FinalQAPhase.INPUT_VALIDATION,
                error_code=error.code.value,
            )
            self.session.commit()
            raise

    # --- identity ---------------------------------------------------------
    def _identity(self, inputs: FinalQAInput) -> tuple[str, dict[str, Any]]:
        """Bind every material input and every configured threshold.

        Changing any selected shot, narration asset, caption asset, final render,
        QA configuration, editorial rubric or provider configuration changes this
        hash, which is what makes a stale report impossible to reuse.
        """
        material = {
            "inputs": inputs.model_dump(mode="json"),
            "configuration": self.options.configuration.model_dump(mode="json"),
            "rubric": rubric_material(),
            "provider": {"first_pass": self.provider.model, "provider": self.provider.name},
            "adjudicator": (
                {"model": self.adjudicator.model, "provider": self.adjudicator.name}
                if self.adjudicator is not None
                else {}
            ),
        }
        return canonical_hash(material), material

    def _create_run(
        self,
        selected: AuthoritativeFinalInputs,
        identity: str,
        material: dict[str, Any],
        idempotency_key: str,
    ) -> FinalEditorialRun:
        inputs = selected.inputs
        run = FinalEditorialRun(
            project_id=inputs.project_id,
            render_job_id=inputs.render_job_id,
            final_render_asset_id=inputs.final_video_asset_id,
            render_manifest_asset_id=inputs.render_manifest_asset_id,
            render_identity=inputs.render_identity,
            final_qa_identity=identity,
            input_hash=canonical_hash(material["inputs"]),
            configuration_hash=configuration_hash(self.options.configuration),
            idempotency_key=idempotency_key,
            status=FinalQAStatus.FINAL_QA_QUEUED.value,
            current_phase=FinalQAPhase.INPUT_VALIDATION.value,
            completed_phases=[],
            pipeline_version=FINAL_QA_PIPELINE_VERSION,
            gate_version=GATE_VERSION,
            first_pass_provider=self.provider.name,
            first_pass_model=self.provider.model,
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        return run

    # --- orchestration ----------------------------------------------------
    async def _run(
        self, run: FinalEditorialRun, selected: AuthoritativeFinalInputs
    ) -> FinalEditorialResult:
        inputs = selected.inputs
        configuration = self.options.configuration
        self.repository.checkpoint(
            run,
            status=FinalQAStatus.FINAL_QA_VALIDATING_INPUTS,
            phase=FinalQAPhase.DETERMINISTIC_MEDIA_QA,
            completed=FinalQAPhase.INPUT_VALIDATION,
        )
        self.session.commit()

        with TemporaryDirectory(prefix="vidgen-final-qa-") as workspace:
            root = Path(workspace)
            render_path = root / "final.mp4"
            self.blob_store.copy_to(selected.final_video_asset.storage_key, render_path)

            # --- deterministic media and audio ----------------------------
            self.repository.checkpoint(
                run,
                status=FinalQAStatus.FINAL_QA_CHECKING_MEDIA,
                phase=FinalQAPhase.DETERMINISTIC_MEDIA_QA,
            )
            self.session.commit()
            measurements = final_deterministic.measure(render_path, configuration)
            media_checks = final_deterministic.evaluate(
                measurements, inputs, configuration, manifest=selected.manifest_payload
            )
            audio_checks, audio_measurements = final_audio.evaluate(
                render_path,
                inputs,
                configuration,
                measurements,
                narration_intervals=list(selected.narration_intervals),
                manifest=selected.manifest_payload,
            )
            self.repository.persist_checks(run, [*media_checks, *audio_checks])
            run.measurement_asset_id = self._store_json(
                run,
                selected,
                existing=run.measurement_asset_id,
                payload={
                    "measurements": measurements.model_dump(mode="json"),
                    "checks": [check.model_dump(mode="json") for check in media_checks],
                    "audio": audio_measurements,
                },
                kind="final_qa_measurements",
                suffix="measurements",
            )
            run.audio_report_asset_id = self._store_json(
                run,
                selected,
                existing=run.audio_report_asset_id,
                payload={"checks": [check.model_dump(mode="json") for check in audio_checks]},
                kind="final_qa_audio_report",
                suffix="audio",
            )
            self.repository.checkpoint(
                run,
                status=FinalQAStatus.FINAL_QA_CHECKING_CAPTIONS,
                phase=FinalQAPhase.CAPTION_QA,
                completed=FinalQAPhase.DETERMINISTIC_MEDIA_QA,
            )
            self.session.commit()

            # --- captions --------------------------------------------------
            caption_checks = self._caption_checks(selected, configuration)
            self.repository.persist_checks(run, list(caption_checks))
            run.caption_report_asset_id = self._store_json(
                run,
                selected,
                existing=run.caption_report_asset_id,
                payload={"checks": [check.model_dump(mode="json") for check in caption_checks]},
                kind="final_qa_caption_report",
                suffix="captions",
            )
            self.repository.checkpoint(
                run,
                status=FinalQAStatus.FINAL_QA_ANALYZING,
                phase=FinalQAPhase.EDITORIAL_ANALYSIS,
                completed=FinalQAPhase.CAPTION_QA,
            )
            self.session.commit()

            all_checks: list[FinalDeterministicCheck] = [
                *media_checks,
                *audio_checks,
                *caption_checks,
            ]
            findings = findings_from_checks(
                all_checks, timeline_duration_us=inputs.timeline_duration_us
            )
            dimensions: list[FinalEditorialDimension] = []
            adjudication: FinalEditorialAdjudication | None = None
            first_pass: FinalEditorialProviderResult | None = None
            provider_request_ids: list[str] = []

            # --- editorial analysis, only when measurement permits it -------
            if any(check.status == "fail" for check in all_checks):
                logger.info(
                    "final QA skipped paid editorial analysis: %d deterministic failure(s)",
                    sum(1 for check in all_checks if check.status == "fail"),
                )
            else:
                frames = extract_frames(render_path, inputs, configuration)
                sheet = build_contact_sheet(frames, columns=configuration.contact_sheet_columns)
                if sheet is not None:
                    frames = [
                        replace(frame, contact_sheet_position=sheet.positions.get(frame.sample_id))
                        for frame in frames
                    ]
                    run.contact_sheet_asset_id = run.contact_sheet_asset_id or self.assets.store(
                        content=sheet.content,
                        kind="final_qa_contact_sheet",
                        media_type=sheet.media_type,
                        project_id=inputs.project_id,
                        parent_asset_ids=self._parents(selected),
                        idempotency_key=f"{run.final_qa_identity}:contact-sheet",
                        metadata=self._provenance(run, selected),
                    ).id
                self.session.commit()
                request = self._request(
                    run,
                    selected,
                    frames,
                    attempt_type="first_pass",
                    attempt_number=1,
                    audio_measurements=audio_measurements,
                )
                first_pass, attempt = await self._evaluate(
                    run, selected, request, frames, first_pass=None
                )
                if attempt.provider_request_id:
                    provider_request_ids.append(attempt.provider_request_id)
                dimensions = list(first_pass.dimension_scores)
                findings.extend(
                    findings_from_provider(
                        first_pass,
                        timeline_duration_us=inputs.timeline_duration_us,
                        frames=frames,
                        attempt_number=1,
                    )
                )
                disputed = (
                    adjudication_triggers(findings, configuration)
                    if self.options.adjudicate and self.adjudicator is not None
                    else []
                )
                if disputed:
                    self.repository.checkpoint(
                        run,
                        status=FinalQAStatus.FINAL_QA_ADJUDICATING,
                        phase=FinalQAPhase.ADJUDICATION,
                        completed=FinalQAPhase.EDITORIAL_ANALYSIS,
                    )
                    self.session.commit()
                    adjudication, second = await self._adjudicate(
                        run, selected, frames, first_pass, disputed, audio_measurements
                    )
                    if second is not None and second.provider_request_id:
                        provider_request_ids.append(second.provider_request_id)
                    findings = apply_adjudication(findings, adjudication)
                    run.adjudication_asset_id = self._store_json(
                        run,
                        selected,
                        existing=run.adjudication_asset_id,
                        payload=adjudication.model_dump(mode="json"),
                        kind="final_qa_adjudication",
                        suffix="adjudication",
                    )

            # --- completion gate -------------------------------------------
            self.repository.checkpoint(
                run,
                status=FinalQAStatus.FINAL_QA_ANALYZING,
                phase=FinalQAPhase.COMPLETION_GATE,
                completed=(
                    FinalQAPhase.ADJUDICATION
                    if adjudication is not None
                    else FinalQAPhase.EDITORIAL_ANALYSIS
                ),
            )
            findings = bound_findings(findings, configuration)
            resolved = self.repository.resolved_finding_ids(run.id)
            gate = decide(
                findings=findings,
                checks=all_checks,
                final_video_asset_id=inputs.final_video_asset_id,
                render_identity=inputs.render_identity,
                resolved_review_ids=resolved,
            )
            routes = remediation_routes(findings)
            report = FinalEditorialReport(
                final_editorial_run_id=run.id,
                project_id=inputs.project_id,
                final_qa_identity=run.final_qa_identity,
                input_hash=run.input_hash,
                configuration_hash=run.configuration_hash,
                inputs=inputs,
                configuration=configuration,
                measurements=measurements,
                deterministic_checks=list(media_checks),
                audio_checks=list(audio_checks),
                caption_checks=list(caption_checks),
                dimensions=dimensions or self._empty_dimensions(),
                findings=findings,
                adjudication=adjudication,
                remediation_routes=routes,
                gate=gate,
                first_pass_provider=self.provider.name,
                first_pass_model=self.provider.model,
                adjudicator_provider=self.adjudicator.name if self.adjudicator else "",
                adjudicator_model=self.adjudicator.model if self.adjudicator else "",
                provider_request_ids=provider_request_ids[:16],
                cost_microusd=run.cost_microusd or 0,
                tool_versions={
                    "ffmpeg": measurements.ffmpeg_version,
                    "ffprobe": measurements.ffprobe_version,
                    "pipeline": FINAL_QA_PIPELINE_VERSION,
                },
                trace_context=dict(self.options.trace_context or {}),
                created_at=datetime.now(UTC),
            )
            # A report is written once for one identity and never overwritten.
            # A resumed run keeps the report its first pass produced, which is
            # what makes the artefact immutable rather than merely idempotent.
            run.report_asset_id = run.report_asset_id or self.assets.store(
                content=json.dumps(
                    report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode(),
                kind="final_qa_report",
                media_type="application/json",
                project_id=inputs.project_id,
                parent_asset_ids=self._parents(selected),
                # The identity is in the key, so a report is written once and a
                # previous report for another identity is never overwritten.
                idempotency_key=f"{run.final_qa_identity}:report",
                metadata=self._provenance(run, selected),
            ).id
            run.final_decision = gate.decision.value
            run.blocking_finding_count = gate.blocking_finding_count
            run.review_finding_count = gate.review_finding_count
            run.warning_finding_count = gate.warning_finding_count
            run.deterministic_failure_count = gate.deterministic_failure_count
            run.remediation_targets = [route.target.value for route in routes]
            self.repository.record_gate(
                run,
                decision=gate.decision,
                blocking_finding_count=gate.blocking_finding_count,
                review_finding_count=gate.review_finding_count,
                deterministic_failure_count=gate.deterministic_failure_count,
                gate_version=GATE_VERSION,
                reasons=list(gate.reasons),
            )
            self.repository.select(run)
            self.repository.checkpoint(
                run,
                status=_status_for(gate.decision),
                phase=FinalQAPhase.COMPLETION_GATE,
                completed=FinalQAPhase.COMPLETION_GATE,
            )
            self.session.commit()
        return self._projection(run, reused=False)

    # --- phases -----------------------------------------------------------
    def _caption_checks(
        self, selected: AuthoritativeFinalInputs, configuration: FinalQAConfiguration
    ) -> list[FinalCaptionCheck]:
        inputs = selected.inputs
        delivered: dict[UUID, bytes] = {}
        hashes: dict[UUID, str] = {}
        for asset_id in inputs.caption_asset_ids:
            asset = self.session.get(Asset, asset_id)
            if asset is None:
                continue
            hashes[asset_id] = asset.sha256
            if self.blob_store.exists(asset.storage_key):
                delivered[asset_id] = self.blob_store.read(asset.storage_key)
        canonical = self._canonical_track(selected, delivered)
        return final_captions.evaluate(
            inputs,
            configuration,
            canonical=canonical,
            delivered=delivered,
            approved_words=list(selected.approved_words),
            narration_segments=list(selected.narration_intervals),
            delivered_hashes=hashes,
            declared_caption_identity=inputs.caption_identity,
            burned_in=inputs.subtitle_mode in {"burn_in", "both"},
        )

    def _canonical_track(
        self, selected: AuthoritativeFinalInputs, delivered: dict[UUID, bytes]
    ) -> CaptionTrack:
        """The delivered selectable track, which is what a viewer actually sees."""
        inputs = selected.inputs
        cues: list[CaptionCue] = []
        for asset_id in inputs.caption_asset_ids:
            content = delivered.get(asset_id)
            if content is None:
                continue
            try:
                cues = final_captions.parse_srt(content)
            except final_captions.CaptionParseError:
                continue
            break
        if not cues:
            cues = [
                CaptionCue(
                    sequence=1,
                    start_us=0,
                    end_us=max(inputs.timeline_duration_us, 1),
                    lines=["(no delivered caption cue could be parsed)"],
                    word_start=0,
                    word_end=1,
                )
            ]
        language = (
            selected.caption_track.language
            if selected.caption_track is not None
            else self.options.configuration.expected_caption_language
        )
        return CaptionTrack(
            caption_track_id=inputs.caption_track_id,
            language=language,
            cues=[cue.model_copy(update={"sequence": index + 1}) for index, cue in enumerate(cues)],
            duration_us=max(inputs.timeline_duration_us, cues[-1].end_us),
        )

    # --- provider ---------------------------------------------------------
    def _request(
        self,
        run: FinalEditorialRun,
        selected: AuthoritativeFinalInputs,
        frames: list[SampledFrame],
        *,
        attempt_type: str,
        attempt_number: int,
        disputed: list[str] | None = None,
        audio_measurements: dict[str, float] | None = None,
    ) -> FinalEditorialProviderRequest:
        inputs = selected.inputs
        manifest = selected.manifest
        attempt_identity = canonical_hash(
            {
                "final_qa_identity": run.final_qa_identity,
                "attempt_type": attempt_type,
                "attempt_number": attempt_number,
                "samples": [str(frame.sample_id) for frame in frames],
                "disputed": disputed or [],
            }
        )
        return FinalEditorialProviderRequest(
            final_qa_identity=run.final_qa_identity,
            attempt_identity=attempt_identity,
            attempt_type=attempt_type,  # type: ignore[arg-type]
            attempt_number=attempt_number,
            project_id=inputs.project_id,
            final_video_asset_id=inputs.final_video_asset_id,
            render_identity=inputs.render_identity,
            timeline_duration_us=inputs.timeline_duration_us,
            script_structure=[
                f"script={inputs.approved_script_id} version={inputs.approved_script_version}"
            ],
            plot_beats=list(selected.plot_beat_summaries)[:128],
            storyboard_timing_summary=[
                f"shot={shot.shot_id} start_us={shot.global_start_us} end_us={shot.global_end_us}"
                for shot in inputs.shots
            ][:500],
            shot_map=[
                f"sequence={shot.sequence} shot={shot.shot_id} asset={shot.video_asset_id}"
                for shot in inputs.shots
            ][:500],
            transition_map=[
                f"sequence={entry.sequence} in={entry.transition_in.kind.value} "
                f"out={entry.transition_out.kind.value}"
                for entry in manifest.shots
            ][:500],
            continuity_summary=[
                f"character_identity_version={item}"
                for item in inputs.character_identity_version_ids
            ][:256],
            video_qa_summary=[
                f"shot={shot.shot_id} qa_result={shot.video_qa_result_id}" for shot in inputs.shots
            ][:500],
            repair_summary=[
                f"shot={shot.shot_id} repair_attempt={shot.selected_repair_attempt_id}"
                for shot in inputs.shots
                if shot.selected_repair_attempt_id is not None
            ][:500],
            caption_timing_summary=[
                f"segment={segment_id} start_us={start} end_us={end}"
                for segment_id, start, end in selected.narration_intervals
            ][:500],
            audio_measurements=dict(audio_measurements or {}),
            samples=[
                frame_evidence(
                    frame,
                    contact_sheet_asset_id=run.contact_sheet_asset_id,
                    explanation="deterministically sampled frame",
                )
                for frame in frames
            ][:64],
            contact_sheet_asset_id=run.contact_sheet_asset_id,
            disputed_findings=(disputed or [])[:32],
            rubric_version=self.options.configuration.editorial_rubric_version,
            prompt_version=self.options.configuration.prompt_version,
            trace_context=dict(self.options.trace_context or {}),
        )

    async def _evaluate(
        self,
        run: FinalEditorialRun,
        selected: AuthoritativeFinalInputs,
        request: FinalEditorialProviderRequest,
        frames: list[SampledFrame],
        *,
        first_pass: FinalEditorialProviderResult | None,
    ) -> tuple[FinalEditorialProviderResult, FinalEditorialProviderAttempt]:
        """Run one bounded provider evaluation with T23 accounting, or reuse one."""
        agent = self.provider if first_pass is None else (self.adjudicator or self.provider)
        phase = FinalQAPhase.EDITORIAL_ANALYSIS if first_pass is None else FinalQAPhase.ADJUDICATION
        existing = self.repository.attempt_by_identity(request.attempt_identity)
        if existing is not None and existing.status == "succeeded" and existing.result_projection:
            return (
                FinalEditorialProviderResult.model_validate(existing.result_projection),
                existing,
            )
        estimated = BASE_CALL_COST + COST_PER_FRAME * len(request.samples)
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=agent.name,
            model=agent.model,
            operation=FINAL_QA_OPERATION,
            input_hash=request.attempt_identity,
            idempotency_key=request.attempt_identity,
            related_entity_id=run.id,
            attempt_number=request.attempt_number,
            estimated_cost=estimated,
        ) as provider_attempt:
            attempt = existing or FinalEditorialProviderAttempt(
                final_editorial_run_id=run.id,
                phase=phase.value,
                attempt_number=request.attempt_number,
                attempt_identity=request.attempt_identity,
                provider_attempt_id=provider_attempt.row.id,
                provider=agent.name,
                model=agent.model,
                input_hash=run.input_hash,
                status="pre_call_checkpoint",
            )
            attempt.provider_attempt_id = provider_attempt.row.id
            self.session.add(attempt)
            self.session.flush()
            reservation_id = self._reserve(run, provider_attempt.row.id, estimated, request)
            self.session.commit()  # durable pre-call checkpoint
            call = FinalEditorialCall(
                request=request,
                frames=tuple(
                    EditorialFrame(
                        sample_id=frame.sample_id,
                        sequence=frame.sequence,
                        timestamp_us=frame.timestamp_us,
                        content=frame.content,
                    )
                    for frame in frames
                ),
                first_pass=first_pass,
            )
            try:
                result = await agent.evaluate(call)
                result = validate_result(
                    result,
                    request,
                    known_sample_ids=[frame.sample_id for frame in frames],
                    known_shot_ids=[shot.shot_id for shot in selected.inputs.shots],
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

    async def _adjudicate(
        self,
        run: FinalEditorialRun,
        selected: AuthoritativeFinalInputs,
        frames: list[SampledFrame],
        first_pass: FinalEditorialProviderResult,
        disputed: list[FinalEditorialFinding],
        audio_measurements: dict[str, float],
    ) -> tuple[FinalEditorialAdjudication, FinalEditorialProviderAttempt | None]:
        """Buy exactly one bounded second opinion over the disputed findings."""
        request = self._request(
            run,
            selected,
            frames,
            attempt_type="adjudication",
            attempt_number=1,
            disputed=[str(finding.finding_id) for finding in disputed],
            audio_measurements=audio_measurements,
        )
        result, attempt = await self._evaluate(
            run, selected, request, frames, first_pass=first_pass
        )
        confidence = result.overall_confidence
        decided = confidence >= ADJUDICATION_CONFIDENCE_FLOOR
        confirmed_codes = {finding.issue_code for finding in result.findings}
        confirmed = [
            finding.finding_id for finding in disputed if finding.issue_code in confirmed_codes
        ]
        dismissed = [
            finding.finding_id for finding in disputed if finding.issue_code not in confirmed_codes
        ]
        return (
            FinalEditorialAdjudication(
                adjudication_id=deterministic_id("adjudication", request.attempt_identity),
                policy_version=ADJUDICATION_POLICY_VERSION,
                triggers=[finding.issue_code.value for finding in disputed][:16],
                disputed_finding_ids=[finding.finding_id for finding in disputed],
                confirmed_finding_ids=confirmed if decided else [],
                dismissed_finding_ids=dismissed if decided else [],
                confidence=confidence,
                decided=decided,
                resulting_decision_hint=(
                    (FinalQADecision.FAIL if confirmed else FinalQADecision.PASS)
                    if decided
                    else FinalQADecision.REVIEW
                ),
                rationale=result.narrative_summary[:1000],
                provider=result.provider,
                model=result.model,
            ),
            attempt,
        )

    # --- T23 accounting ---------------------------------------------------
    def _reserve(
        self,
        run: FinalEditorialRun,
        provider_attempt_id: UUID,
        estimated: Decimal,
        request: FinalEditorialProviderRequest,
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
                idempotency_key=f"{request.attempt_identity}:reservation",
                estimated_amount=estimated,
                currency="USD",
            )
        )
        if reservation.decision in {
            BudgetDecision.DENY_ENTITY_CAP,
            BudgetDecision.DENY_HARD_CAP,
            BudgetDecision.UNKNOWN_PRICE_REVIEW,
        }:
            raise BudgetExceededError(f"final editorial QA denied: {reservation.decision}")
        return reservation.reservation_id

    def _reconcile(
        self,
        reservation_id: UUID | None,
        request: FinalEditorialProviderRequest,
        actual: Decimal,
        *,
        billable: bool = True,
    ) -> None:
        if reservation_id is None:
            return
        self.costs.reconcile(
            reservation_id,
            f"{request.attempt_identity}:reconciliation",
            actual,
            billable=billable,
        )

    # --- persistence helpers ----------------------------------------------
    def _parents(self, selected: AuthoritativeFinalInputs) -> tuple[UUID, ...]:
        inputs = selected.inputs
        parents = [
            inputs.final_video_asset_id,
            inputs.render_manifest_asset_id,
            *[shot.video_asset_id for shot in inputs.shots],
            *inputs.narration_asset_ids,
            *inputs.caption_asset_ids,
        ]
        seen: list[UUID] = []
        for parent in parents:
            if parent not in seen and self.session.get(Asset, parent) is not None:
                seen.append(parent)
        return tuple(seen[:32])

    def _provenance(
        self, run: FinalEditorialRun, selected: AuthoritativeFinalInputs
    ) -> dict[str, Any]:
        inputs = selected.inputs
        return {
            "project_id": str(inputs.project_id),
            "final_render_asset_id": str(inputs.final_video_asset_id),
            "render_manifest_asset_id": str(inputs.render_manifest_asset_id),
            "selected_shot_asset_ids": [str(shot.video_asset_id) for shot in inputs.shots],
            "narration_asset_ids": [str(item) for item in inputs.narration_asset_ids],
            "caption_asset_ids": [str(item) for item in inputs.caption_asset_ids],
            "video_qa_result_ids": [str(shot.video_qa_result_id) for shot in inputs.shots],
            "repair_run_ids": [
                str(shot.repair_run_id) for shot in inputs.shots if shot.repair_run_id
            ],
            "character_identity_version_ids": [
                str(item) for item in inputs.character_identity_version_ids
            ],
            "configuration_hash": run.configuration_hash,
            "input_hash": run.input_hash,
            "final_qa_identity": run.final_qa_identity,
            "idempotency_key": run.idempotency_key,
            "check_versions": {
                "deterministic": self.options.configuration.deterministic_check_version,
                "audio": self.options.configuration.audio_check_version,
                "caption": self.options.configuration.caption_check_version,
                "rubric": self.options.configuration.editorial_rubric_version,
                "gate": GATE_VERSION,
            },
            "provider": self.provider.name,
            "model": self.provider.model,
            "trace_context": dict(self.options.trace_context or {}),
        }

    def _store_json(
        self,
        run: FinalEditorialRun,
        selected: AuthoritativeFinalInputs,
        *,
        payload: dict[str, Any],
        kind: str,
        suffix: str,
        existing: UUID | None = None,
    ) -> UUID:
        """Write one QA artefact, or return the one this run already wrote.

        The payloads carry measurement timestamps, so re-storing on a resumed
        run would collide with its own idempotency key. Reusing the first
        artefact is also the correct behaviour: the identity binds every
        material input, so a second measurement of the same file says the same
        thing.
        """
        if existing is not None:
            return existing
        stored = self.assets.store(
            content=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            kind=kind,
            media_type="application/json",
            project_id=run.project_id,
            parent_asset_ids=self._parents(selected),
            idempotency_key=f"{run.final_qa_identity}:{suffix}",
            metadata=self._provenance(run, selected),
        )
        return stored.id

    def _empty_dimensions(self) -> list[FinalEditorialDimension]:
        """A deterministic failure short-circuits analysis; dimensions stay unscored."""
        return [
            FinalEditorialDimension(
                category=category,
                applicable=False,
                score=0.0,
                confidence=0.0,
                summary="not evaluated: deterministic checks did not permit analysis",
            )
            for category in EDITORIAL_DIMENSIONS
        ]

    def _projection(self, run: FinalEditorialRun, *, reused: bool) -> FinalEditorialResult:
        checks = self.repository.checks(run.id)
        attempts = self.repository.attempts(run.id)
        adjudicated = any(attempt.phase == FinalQAPhase.ADJUDICATION.value for attempt in attempts)
        routes = [FinalRemediationTarget(value) for value in list(run.remediation_targets or [])][
            :16
        ]
        return FinalEditorialResult(
            final_editorial_run_id=run.id,
            project_id=run.project_id,
            final_video_asset_id=run.final_render_asset_id,
            render_manifest_asset_id=run.render_manifest_asset_id,
            final_qa_identity=run.final_qa_identity,
            input_hash=run.input_hash,
            configuration_hash=run.configuration_hash,
            status=FinalQAStatus(run.status),
            phase=FinalQAPhase(run.current_phase),
            decision=FinalQADecision(run.final_decision) if run.final_decision else None,
            deterministic_check_count=sum(
                1 for check in checks if check.check_type in {"media", "timeline", "manifest"}
            ),
            deterministic_failure_count=sum(
                1
                for check in checks
                if check.status == "fail" and check.check_type in {"media", "timeline", "manifest"}
            ),
            audio_check_count=sum(1 for check in checks if check.check_type == "audio"),
            audio_failure_count=sum(
                1 for check in checks if check.check_type == "audio" and check.status == "fail"
            ),
            caption_check_count=sum(1 for check in checks if check.check_type == "caption"),
            caption_failure_count=sum(
                1 for check in checks if check.check_type == "caption" and check.status == "fail"
            ),
            blocking_finding_count=run.blocking_finding_count or 0,
            review_finding_count=run.review_finding_count or 0,
            warning_finding_count=run.warning_finding_count or 0,
            remediation_targets=routes,
            first_pass_provider=run.first_pass_provider or "",
            first_pass_model=run.first_pass_model or "",
            adjudicated=adjudicated,
            cost_microusd=run.cost_microusd or 0,
            report_asset_id=run.report_asset_id,
            error_code=run.error_code,
            reused=reused,
        )


def _status_for(decision: FinalQADecision) -> FinalQAStatus:
    if decision is FinalQADecision.PASS:
        return FinalQAStatus.FINAL_QA_PASSED
    if decision is FinalQADecision.REVIEW:
        return FinalQAStatus.FINAL_QA_REVIEW_REQUIRED
    return FinalQAStatus.FINAL_QA_FAILED


def caption_identity_of(track: CaptionTrack) -> str:
    """Re-exported so callers do not import the renderer's caption module."""
    return caption_track_identity(track)


__all__ = [
    "BASE_CALL_COST",
    "COST_PER_FRAME",
    "FINAL_QA_OPERATION",
    "FinalEditorialPipeline",
    "FinalQAOptions",
    "caption_identity_of",
]
