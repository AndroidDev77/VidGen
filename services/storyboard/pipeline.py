"""Restartable segment-at-a-time T13 orchestration.

Separation of concerns is deliberate: the Storyboard Director proposes, the
retimer decides timing, the validator judges, this module persists and
orchestrates, and Temporal only ever carries IDs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from opentelemetry import trace
from sqlalchemy.orm import Session

from services.storyboard.boundaries import (
    approved_boundaries,
    word_boundaries,
    word_timing_hash,
)
from services.storyboard.canonicalize import (
    canonical_hash,
    canonical_json,
    seconds_to_us,
    stable_id,
)
from services.storyboard.director import InstrumentedStoryboardDirector
from services.storyboard.providers import (
    DIRECTOR_VERSION,
    PROMPT_VERSION,
    StoryboardDirector,
    load_capability_profile,
)
from services.storyboard.retimer import (
    RetimerConfig,
    RetimerError,
    SegmentTiming,
    ShotTiming,
    retime_segment,
)
from services.storyboard.validator import (
    VALIDATOR_VERSION,
    SegmentValidationContext,
    build_report,
    validate_outgoing_handoff,
    validate_proposals,
    validate_segment_timing,
    validate_storyboard,
)
from vidgen.contracts.episode_analysis import StructuredNote
from vidgen.contracts.storyboard import (
    CONTRACT_VERSION,
    ContinuityState,
    NarrationBoundary,
    Storyboard,
    StoryboardProviderRequest,
    StoryboardProviderResult,
    StoryboardResult,
    StoryboardSegment,
    StoryboardShot,
    StoryboardShotProposal,
    StoryboardSourceReference,
    StoryboardValidationDiagnostic,
    StoryboardValidationReport,
    TimingAdjustment,
    TimingManifest,
    TimingManifestEntry,
    TransitionPlan,
    VisualProviderCapability,
)
from vidgen.db.models import Asset, Project
from vidgen.db.narration_models import NarrationSegment
from vidgen.db.script_models import ScriptSegment
from vidgen.db.storyboard_models import (
    StoryboardRepairAttempt,
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.storyboard_repository import (
    AuthoritativeInputs,
    StoryboardLineageError,
    StoryboardRepository,
)
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.metrics import Metrics

PIPELINE_VERSION = "storyboard/1.0.0"
STORYBOARD_MEDIA_TYPE = "application/vnd.vidgen.storyboard+json"
TIMING_MANIFEST_MEDIA_TYPE = "application/vnd.vidgen.storyboard-timing+json"
VALIDATION_REPORT_MEDIA_TYPE = "application/vnd.vidgen.storyboard-validation+json"
#: Validation reports above this size are stored as their own artifact.
LARGE_REPORT_BYTES = 64 * 1024
DEFAULT_MAX_REPAIR_ATTEMPTS = 2


class StoryboardValidationFailed(RuntimeError):
    """A segment could not be repaired into a valid plan."""

    def __init__(self, report: StoryboardValidationReport) -> None:
        codes = sorted({item.code for item in report.diagnostics if item.severity == "error"})
        super().__init__("storyboard segment validation failed: " + ", ".join(codes))
        self.report = report


class StoryboardPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: StoryboardDirector,
        *,
        capability_profile_id: str | None = None,
        capability_override: dict[str, Any] | None = None,
        retimer_config: RetimerConfig | None = None,
        metrics: Metrics | None = None,
        cancellation_check: Callable[[], bool] | None = None,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.capability_profile_id = capability_profile_id
        self.capability_override = capability_override
        self.retimer_config = retimer_config or RetimerConfig()
        self.metrics = metrics or Metrics()
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.storyboard")
        self.cancellation_check = cancellation_check or (lambda: False)
        self.max_repair_attempts = max(0, min(max_repair_attempts, 2))
        self.repo = StoryboardRepository(session)
        self.assets = AssetService(session, blob_store)
        self.director = InstrumentedStoryboardDirector(
            session, provider, tracer=self.tracer, metrics=self.metrics
        )
        self._estimated_cost = Decimal("0")
        self._actual_cost = Decimal("0")
        self._episode_payload: dict[str, Any] | None = None

    # -- entry point -------------------------------------------------------------

    async def process(self, *, project_id: UUID, idempotency_key: str) -> StoryboardResult:
        try:
            return await self._process(project_id=project_id, idempotency_key=idempotency_key)
        except BaseException as error:
            # A failed flush leaves the session unusable; roll back before recording
            # the terminal status so the failure itself is always persisted.
            self.session.rollback()
            run = self.repo.run_by_key(project_id, idempotency_key)
            project = self.session.get(Project, project_id)
            code = getattr(error, "code", None) or type(error).__name__
            if run is not None:
                run.status = "storyboard_failed"
                run.error_code = str(code)[:128]
            if project is not None:
                project.status = "storyboard_failed"
            self.session.commit()
            raise

    async def _process(self, *, project_id: UUID, idempotency_key: str) -> StoryboardResult:
        inputs = self.repo.authoritative_inputs(project_id)
        capability = load_capability_profile(
            self.capability_profile_id or self._configured_profile_id(inputs.project),
            self.capability_override or self._configured_override(inputs.project),
        )
        material = self._input_material(inputs, capability)
        input_hash = canonical_hash(material)
        run = self._resolve_run(inputs, capability, idempotency_key, input_hash, material)
        if run.status == "storyboard_complete":
            # An identical completed run returns without any new provider submission.
            return self._result(run)
        project = inputs.project
        project.status = run.status = "storyboard_directing"
        self.session.commit()

        continuity = ContinuityState()
        global_start = 0
        segment_timings: list[tuple[StoryboardSegmentCheckpoint, list[ShotTiming]]] = []
        adjustments: list[TimingAdjustment] = []
        residual_total = 0
        run_warnings: list[StructuredNote] = []
        for script_segment, narration_segment in zip(
            inputs.script_segments, inputs.narration_segments, strict=True
        ):
            self._check_cancelled()
            checkpoint, timing, continuity, warnings = await self._process_segment(
                run=run,
                project=project,
                inputs=inputs,
                capability=capability,
                material=material,
                script_segment=script_segment,
                narration_segment=narration_segment,
                incoming=continuity,
                global_start_us=global_start,
            )
            segment_timings.append((checkpoint, timing.shots))
            adjustments.extend(timing.adjustments)
            residual_total += timing.residual_allocation_us
            run_warnings.extend(warnings)
            global_start += checkpoint.narration_duration_us

        project.status = run.status = "storyboard_validating"
        self.session.commit()
        return self._finalize_run(
            run=run,
            project=project,
            inputs=inputs,
            capability=capability,
            material=material,
            segment_timings=segment_timings,
            adjustments=adjustments,
            residual_total=residual_total,
            total_duration_us=global_start,
            warnings=run_warnings,
        )

    # -- authoritative identity --------------------------------------------------

    @staticmethod
    def _configured_profile_id(project: Project) -> str | None:
        settings = project.settings if isinstance(project.settings, dict) else {}
        storyboard = settings.get("storyboard")
        if isinstance(storyboard, dict):
            value = storyboard.get("capability_profile_id")
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _configured_override(project: Project) -> dict[str, Any] | None:
        settings = project.settings if isinstance(project.settings, dict) else {}
        storyboard = settings.get("storyboard")
        if isinstance(storyboard, dict):
            override = storyboard.get("capability_profile")
            if isinstance(override, dict):
                return override
        return None

    def _input_material(
        self, inputs: AuthoritativeInputs, capability: VisualProviderCapability
    ) -> dict[str, Any]:
        """Everything the storyboard identity binds. Order is significant."""
        narration_assets = [
            self.session.get(Asset, segment.normalized_asset_id)
            for segment in inputs.narration_segments
        ]
        if any(asset is None for asset in narration_assets):
            raise StoryboardLineageError(
                "narration_asset_missing", "a selected narration audio asset no longer exists"
            )
        script_asset = self.session.get(Asset, inputs.script.canonical_script_asset_id)
        if script_asset is None:
            raise StoryboardLineageError(
                "script_asset_missing", "the approved script's canonical asset no longer exists"
            )
        return {
            "project_id": str(inputs.project.id),
            "episode_model_id": str(inputs.episode_model.id),
            "episode_model_hash": inputs.episode_model_hash,
            "script_id": str(inputs.script.id),
            "script_version": inputs.script.version,
            "script_hash": script_asset.sha256,
            "narration_run_id": str(inputs.narration_run.id),
            "narration_asset_ids": [str(asset.id) for asset in narration_assets if asset],
            "narration_asset_hashes": [asset.sha256 for asset in narration_assets if asset],
            "measured_durations_us": [
                seconds_to_us(segment.duration_seconds or 0)
                for segment in inputs.narration_segments
            ],
            "word_timing_hashes": [
                word_timing_hash(list(segment.word_timings or []))
                for segment in inputs.narration_segments
            ],
            "capability_profile_id": capability.capability_profile_id,
            "capability_hash": capability.capability_hash,
            "prompt_version": PROMPT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "director_version": DIRECTOR_VERSION,
            "retimer_version": self.retimer_config.version,
            "retimer_config": self.retimer_config.material(),
            "validator_version": VALIDATOR_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "provider": self.provider.name,
            "model": self.provider.model,
        }

    def _resolve_run(
        self,
        inputs: AuthoritativeInputs,
        capability: VisualProviderCapability,
        idempotency_key: str,
        input_hash: str,
        material: dict[str, Any],
    ) -> StoryboardRun:
        run = self.repo.run_by_key(inputs.project.id, idempotency_key)
        if run is not None:
            if run.input_hash != input_hash:
                raise StoryboardLineageError(
                    "idempotency_key_reused",
                    "this idempotency key was already used with different storyboard inputs; "
                    "use a new key or reselect the matching upstream versions",
                )
            return run
        run = StoryboardRun(
            id=stable_id("run", inputs.project.id, idempotency_key, input_hash),
            project_id=inputs.project.id,
            episode_model_id=inputs.episode_model.id,
            script_id=inputs.script.id,
            script_version=inputs.script.version,
            narration_run_id=inputs.narration_run.id,
            capability_profile_id=capability.capability_profile_id,
            capability_hash=capability.capability_hash,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            status="storyboard_queued",
            provider=self.provider.name,
            model=self.provider.model,
            contract_version=CONTRACT_VERSION,
            director_version=DIRECTOR_VERSION,
            prompt_version=PROMPT_VERSION,
            retimer_version=self.retimer_config.version,
            version=self.repo.next_version(inputs.project.id),
            parameters=material,
        )
        self.session.add(run)
        self.session.flush()
        inputs.project.status = "storyboard_queued"
        self.session.commit()
        return run

    def _check_cancelled(self) -> None:
        if self.cancellation_check():
            raise RuntimeError("storyboard activity cancelled")

    # -- per-segment orchestration -----------------------------------------------

    async def _process_segment(
        self,
        *,
        run: StoryboardRun,
        project: Project,
        inputs: AuthoritativeInputs,
        capability: VisualProviderCapability,
        material: dict[str, Any],
        script_segment: ScriptSegment,
        narration_segment: NarrationSegment,
        incoming: ContinuityState,
        global_start_us: int,
    ) -> tuple[StoryboardSegmentCheckpoint, SegmentTiming, ContinuityState, list[StructuredNote]]:
        duration_us = seconds_to_us(narration_segment.duration_seconds or 0)
        timings = word_boundaries(list(narration_segment.word_timings or []), duration_us)
        approved = approved_boundaries(
            timings,
            text=script_segment.text,
            joke_annotations=list(script_segment.joke_annotations or []),
        )
        segment_material = {
            "run_input_hash": run.input_hash,
            "sequence": script_segment.sequence,
            "script_segment_id": str(script_segment.id),
            "narration_segment_id": str(narration_segment.id),
            "content_hash": script_segment.content_hash,
            "duration_us": duration_us,
            "word_timing_hash": word_timing_hash(list(narration_segment.word_timings or [])),
            "incoming_continuity": incoming.model_dump(mode="json"),
        }
        segment_hash = canonical_hash(segment_material)
        checkpoint = self._checkpoint(
            run=run,
            script_segment=script_segment,
            narration_segment=narration_segment,
            segment_hash=segment_hash,
            duration_us=duration_us,
            global_start_us=global_start_us,
            incoming=incoming,
        )
        if checkpoint.status == "complete":
            timing = self._recover_timing(checkpoint)
            outgoing = ContinuityState.model_validate(checkpoint.outgoing_continuity or {})
            warnings = [
                StructuredNote(code="retimer", message=message) for message in timing.warnings
            ]
            return checkpoint, timing, outgoing, warnings

        context = self._validation_context(
            inputs=inputs,
            capability=capability,
            script_segment=script_segment,
            sequence=script_segment.sequence,
            duration_us=duration_us,
            word_count=len(timings),
            incoming=incoming,
        )
        diagnostics, first_attempt = self._resume_point(checkpoint)
        report: StoryboardValidationReport | None = None
        for attempt in range(first_attempt, self.max_repair_attempts + 2):
            self._check_cancelled()
            project.status = "storyboard_repairing" if attempt > 1 else "storyboard_directing"
            checkpoint.attempt_count = attempt
            checkpoint.status = "directing"
            repair = self._repair_row(checkpoint, attempt, diagnostics)
            self.session.commit()  # durable pre-provider checkpoint

            result = await self._direct(
                run=run,
                inputs=inputs,
                capability=capability,
                checkpoint=checkpoint,
                script_segment=script_segment,
                narration_segment=narration_segment,
                timings=timings,
                approved=approved,
                duration_us=duration_us,
                incoming=incoming,
                diagnostics=diagnostics,
                attempt=attempt,
                repair=repair,
            )
            project.status = "storyboard_retiming"
            timing, diagnostics = self._retime_and_validate(
                result=result,
                context=context,
                capability=capability,
                timings=timings,
                approved=approved,
            )
            project.status = "storyboard_validating"
            report = build_report(
                diagnostics,
                checked_segment_sequences=[script_segment.sequence],
                covered_duration_us=sum(shot.usable_duration_us for shot in timing.shots)
                if timing
                else 0,
                expected_duration_us=duration_us,
            )
            checkpoint.validation_report = report.model_dump(mode="json")
            if repair is not None:
                repair.validation_result = report.model_dump(mode="json")
                repair.status = "valid" if report.valid else "invalid"
                repair.completed_at = datetime.now(UTC)
            if report.valid and timing is not None:
                outgoing = result.expected_outgoing_continuity
                self._persist_segment(
                    run=run,
                    checkpoint=checkpoint,
                    capability=capability,
                    result=result,
                    timing=timing,
                    script_segment=script_segment,
                    narration_segment=narration_segment,
                    global_start_us=global_start_us,
                    outgoing=outgoing,
                )
                self.session.commit()
                warnings = [
                    StructuredNote(code="retimer", message=message) for message in timing.warnings
                ]
                return checkpoint, timing, outgoing, warnings
            checkpoint.status = "invalid"
            checkpoint.error_code = ",".join(
                sorted({item.code for item in diagnostics if item.severity == "error"})
            )[:128]
            self.session.commit()
            if not all(item.repairable for item in diagnostics if item.severity == "error"):
                break
        assert report is not None
        raise StoryboardValidationFailed(report)

    @staticmethod
    def _resume_point(
        checkpoint: StoryboardSegmentCheckpoint,
    ) -> tuple[list[StoryboardValidationDiagnostic], int]:
        """Where a resumed segment should pick the repair loop back up.

        Restarting at attempt one would re-submit - and pay for - a provider call
        whose result is already known to have failed validation. The stored result
        names the last attempt that actually *completed*, which is what the resume
        point keys on: ``attempt_count`` counts attempts *started*, so an attempt
        interrupted before its provider responded must be retried, not skipped.
        """
        stored = checkpoint.provider_result
        if not isinstance(stored, dict) or stored.get("input_hash") != checkpoint.input_hash:
            return [], 1
        completed = int(stored.get("attempt", 0))
        if completed < 1:
            return [], 1
        report = checkpoint.validation_report
        if not isinstance(report, dict):
            # The provider responded but validation never ran; redo that attempt,
            # which recovers the stored result instead of re-requesting it.
            return [], completed
        parsed = StoryboardValidationReport.model_validate(report)
        if parsed.valid:
            return [], completed
        errors = [item for item in parsed.diagnostics if item.severity == "error"]
        if not errors or not all(item.repairable for item in errors):
            return [], 1
        return parsed.diagnostics, completed + 1

    def _checkpoint(
        self,
        *,
        run: StoryboardRun,
        script_segment: ScriptSegment,
        narration_segment: NarrationSegment,
        segment_hash: str,
        duration_us: int,
        global_start_us: int,
        incoming: ContinuityState,
    ) -> StoryboardSegmentCheckpoint:
        existing = self.repo.checkpoint(run.id, script_segment.sequence)
        if existing is not None:
            if existing.input_hash != segment_hash:
                # An upstream repair changed this segment's inputs; its own
                # checkpoint and shots are rebuilt, later segments are untouched.
                self._reset_checkpoint(existing, segment_hash, incoming)
            return existing
        checkpoint = StoryboardSegmentCheckpoint(
            id=stable_id("segment", run.id, script_segment.sequence),
            storyboard_run_id=run.id,
            script_segment_id=script_segment.id,
            narration_segment_id=narration_segment.id,
            sequence=script_segment.sequence,
            input_hash=segment_hash,
            idempotency_key=f"{run.id}:segment:{segment_hash}",
            status="pending",
            attempt_count=0,
            repair_attempt_count=0,
            narration_duration_us=duration_us,
            global_start_us=global_start_us,
            incoming_continuity=incoming.model_dump(mode="json"),
        )
        self.session.add(checkpoint)
        self.session.flush()
        return checkpoint

    def _reset_checkpoint(
        self,
        checkpoint: StoryboardSegmentCheckpoint,
        segment_hash: str,
        incoming: ContinuityState,
    ) -> None:
        # Renumbering is run-wide, so every later segment's canonical shots are
        # rebuilt too. Their validated provider results are kept, so no provider is
        # called again and only the affected segment is actually re-directed.
        # Each rebuilt checkpoint replays from attempt one, so its attempt counters
        # and superseded repair rows are cleared with it: leaving a stale
        # ``repair_attempt_count`` behind would break the checkpoint's own
        # ``repair_attempt_count <= attempt_count`` constraint on the next commit.
        for later in self.repo.checkpoints_from(checkpoint.storyboard_run_id, checkpoint.sequence):
            for row in self.repo.segment_shots(later.id):
                self.session.delete(row)
            for repair in self.repo.repair_attempts(later.id):
                self.session.delete(repair)
            later.attempt_count = 0
            later.repair_attempt_count = 0
            if later.id != checkpoint.id:
                later.status = "pending"
                later.outgoing_continuity = None
        checkpoint.input_hash = segment_hash
        checkpoint.idempotency_key = f"{checkpoint.storyboard_run_id}:segment:{segment_hash}"
        checkpoint.status = "pending"
        checkpoint.provider_result = None
        checkpoint.provider_request_id = None
        checkpoint.validation_report = None
        checkpoint.incoming_continuity = incoming.model_dump(mode="json")
        checkpoint.outgoing_continuity = None
        self.session.flush()

    def _repair_row(
        self,
        checkpoint: StoryboardSegmentCheckpoint,
        attempt: int,
        diagnostics: list[StoryboardValidationDiagnostic],
    ) -> StoryboardRepairAttempt | None:
        if attempt < 2:
            return None
        number = attempt - 1
        existing = next(
            (
                item
                for item in self.repo.repair_attempts(checkpoint.id)
                if item.attempt_number == number
            ),
            None,
        )
        if existing is not None:
            return existing
        repair = StoryboardRepairAttempt(
            id=stable_id("repair", checkpoint.id, number),
            segment_checkpoint_id=checkpoint.id,
            attempt_number=number,
            idempotency_key=f"{checkpoint.idempotency_key}:repair:{number}",
            input_diagnostics=[item.model_dump(mode="json") for item in diagnostics],
            status="pending",
        )
        self.session.add(repair)
        checkpoint.repair_attempt_count = number
        self.session.flush()
        return repair

    async def _direct(
        self,
        *,
        run: StoryboardRun,
        inputs: AuthoritativeInputs,
        capability: VisualProviderCapability,
        checkpoint: StoryboardSegmentCheckpoint,
        script_segment: ScriptSegment,
        narration_segment: NarrationSegment,
        timings: list[NarrationBoundary],
        approved: list[NarrationBoundary],
        duration_us: int,
        incoming: ContinuityState,
        diagnostics: list[StoryboardValidationDiagnostic],
        attempt: int,
        repair: StoryboardRepairAttempt | None,
    ) -> StoryboardProviderResult:
        key = f"{checkpoint.idempotency_key}:{attempt}"
        recovered = self._recover_provider_result(checkpoint, attempt)
        if recovered is not None:
            return recovered
        request = StoryboardProviderRequest(
            idempotency_key=key,
            project_id=inputs.project.id,
            episode_model_id=inputs.episode_model.id,
            episode_model_hash=inputs.episode_model_hash,
            script_id=inputs.script.id,
            script_version=inputs.script.version,
            script_segment_id=script_segment.id,
            segment_sequence=script_segment.sequence,
            narration_run_id=inputs.narration_run.id,
            narration_segment_id=narration_segment.id,
            narration_asset_id=narration_segment.normalized_asset_id or narration_segment.id,
            measured_duration_us=duration_us,
            narration_text=script_segment.text[:8000],
            word_timings=timings,
            approved_boundaries=approved,
            evidence_references=self._evidence_references(inputs, script_segment),
            available_character_ids=self._available_characters(inputs),
            available_location_ids=self._available_locations(inputs),
            anonymous_speaker_label=script_segment.anonymous_speaker_label,
            incoming_continuity=incoming,
            capability=capability,
            contract_version=CONTRACT_VERSION,
            prompt_version=PROMPT_VERSION,
            validation_diagnostics=diagnostics,
            trace_context=self._trace_context(),
            attempt_number=attempt,
        )
        expected_shots = max(1, duration_us // 4_000_000)
        outcome = await self.director.direct(
            request,
            input_hash=checkpoint.input_hash,
            related_entity_id=checkpoint.id,
            expected_shots=expected_shots,
        )
        self._estimated_cost += outcome.estimated_cost
        self._actual_cost += outcome.actual_cost
        checkpoint.provider_request_id = outcome.result.provider_request_id
        checkpoint.provider_result = {
            "idempotency_key": key,
            "input_hash": checkpoint.input_hash,
            "attempt": attempt,
            "validated": False,
            "result": outcome.result.model_dump(mode="json"),
        }
        checkpoint.status = "directed"
        if repair is not None:
            repair.provider_attempt_id = outcome.provider_attempt_id
            repair.provider_request_id = outcome.result.provider_request_id
            repair.status = "directed"
        self.session.commit()  # durable provider-response recovery checkpoint
        return outcome.result

    @staticmethod
    def _recover_provider_result(
        checkpoint: StoryboardSegmentCheckpoint, attempt: int
    ) -> StoryboardProviderResult | None:
        """Reuse a durably stored provider result instead of paying for it twice.

        Two cases qualify, and only these two. A run interrupted between the
        provider response and validation resumes at the same attempt. A checkpoint
        rebuilt only to renumber shots replays a result that already validated
        against these exact inputs. A result that failed validation is never
        reused, because the repair attempt exists precisely to replace it.
        """
        stored = checkpoint.provider_result
        if not isinstance(stored, dict) or stored.get("input_hash") != checkpoint.input_hash:
            return None
        if stored.get("attempt") != attempt and not stored.get("validated"):
            return None
        return StoryboardProviderResult.model_validate(stored["result"])

    def _retime_and_validate(
        self,
        *,
        result: StoryboardProviderResult,
        context: SegmentValidationContext,
        capability: VisualProviderCapability,
        timings: list[NarrationBoundary],
        approved: list[NarrationBoundary],
    ) -> tuple[SegmentTiming, list[StoryboardValidationDiagnostic]]:
        diagnostics = validate_proposals(list(result.proposals), context)
        diagnostics.extend(
            validate_outgoing_handoff(
                list(result.proposals), result.expected_outgoing_continuity, context
            )
        )
        if any(item.severity == "error" for item in diagnostics):
            return SegmentTiming(shots=[]), diagnostics
        try:
            timing = retime_segment(
                segment_sequence=context.segment_sequence,
                narration_duration_us=context.narration_duration_us,
                word_timings=timings,
                approved_boundaries=approved,
                proposals=list(result.proposals),
                capability=capability,
                config=self.retimer_config,
            )
        except RetimerError as error:
            return SegmentTiming(shots=[]), [*diagnostics, error.diagnostic]
        diagnostics.extend(validate_segment_timing(timing.shots, context))
        return timing, diagnostics

    def _validation_context(
        self,
        *,
        inputs: AuthoritativeInputs,
        capability: VisualProviderCapability,
        script_segment: ScriptSegment,
        sequence: int,
        duration_us: int,
        word_count: int,
        incoming: ContinuityState,
    ) -> SegmentValidationContext:
        evidence_ids: frozenset[UUID] = frozenset()
        if inputs.evidence_package is not None:
            evidence_ids = self.repo.evidence_scene_ids(inputs.evidence_package.id) | {
                inputs.evidence_package.id
            }
        return SegmentValidationContext(
            segment_sequence=sequence,
            narration_duration_us=duration_us,
            word_count=word_count,
            capability=capability,
            available_character_ids=frozenset(self._available_characters(inputs)),
            available_location_ids=frozenset(self._available_locations(inputs)),
            valid_evidence_ids=evidence_ids,
            incoming_continuity=incoming,
            anonymous_speaker=script_segment.speaker_kind == "anonymous"
            or script_segment.anonymous_speaker_label is not None,
        )

    # -- reference resolution ----------------------------------------------------

    def _episode_model_payload(self, inputs: AuthoritativeInputs) -> dict[str, Any]:
        if self._episode_payload is not None:
            return self._episode_payload
        asset = self.session.get(Asset, inputs.episode_model.canonical_analysis_asset_id)
        if asset is None:
            raise StoryboardLineageError(
                "episode_model_asset_missing", "the selected episode model asset no longer exists"
            )
        payload = json.loads(self.blob_store.read(asset.storage_key).decode())
        self._episode_payload = payload if isinstance(payload, dict) else {}
        return self._episode_payload

    def _available_characters(self, inputs: AuthoritativeInputs) -> list[UUID]:
        payload = self._episode_model_payload(inputs)
        return [
            UUID(str(item["character_id"]))
            for item in payload.get("characters", [])
            if isinstance(item, dict) and item.get("character_id") and not item.get("anonymous")
        ]

    def _available_locations(self, inputs: AuthoritativeInputs) -> list[UUID]:
        payload = self._episode_model_payload(inputs)
        return [
            UUID(str(item["location_id"]))
            for item in payload.get("locations", [])
            if isinstance(item, dict) and item.get("location_id")
        ]

    def _evidence_references(
        self, inputs: AuthoritativeInputs, script_segment: ScriptSegment
    ) -> list[StoryboardSourceReference]:
        if inputs.evidence_package is None:
            return []
        valid = self.repo.evidence_scene_ids(inputs.evidence_package.id)
        references = [
            StoryboardSourceReference(
                reference_type="evidence_package", reference_id=inputs.evidence_package.id
            )
        ]
        for raw in list(script_segment.source_scene_ids or [])[:16]:
            try:
                scene_id = UUID(str(raw))
            except ValueError:
                continue
            if scene_id in valid:
                references.append(
                    StoryboardSourceReference(
                        reference_type="scene_evidence", reference_id=scene_id
                    )
                )
        return references

    @staticmethod
    def _trace_context() -> dict[str, str]:
        span = trace.get_current_span().get_span_context()
        if not span.is_valid:
            return {}
        return {
            "traceparent": (f"00-{format(span.trace_id, '032x')}-{format(span.span_id, '016x')}-01")
        }

    # -- persistence -------------------------------------------------------------

    def _persist_segment(
        self,
        *,
        run: StoryboardRun,
        checkpoint: StoryboardSegmentCheckpoint,
        capability: VisualProviderCapability,
        result: StoryboardProviderResult,
        timing: SegmentTiming,
        script_segment: ScriptSegment,
        narration_segment: NarrationSegment,
        global_start_us: int,
        outgoing: ContinuityState,
    ) -> None:
        for row in self.repo.segment_shots(checkpoint.id):
            self.session.delete(row)
        self.session.flush()
        base = self.repo.shot_count_before(run.id, checkpoint.sequence)
        proposals = {item.proposal_sequence: item for item in result.proposals}
        for shot_timing in timing.shots:
            proposal = proposals[shot_timing.proposal_sequence]
            shot = self._build_shot(
                run=run,
                checkpoint=checkpoint,
                capability=capability,
                proposal=proposal,
                timing=shot_timing,
                script_segment=script_segment,
                narration_segment=narration_segment,
                global_start_us=global_start_us,
                global_sequence=base + shot_timing.shot_sequence,
            )
            self.session.add(
                StoryboardShotRecord(
                    id=stable_id("shot_row", checkpoint.id, shot_timing.shot_sequence),
                    storyboard_run_id=run.id,
                    segment_checkpoint_id=checkpoint.id,
                    stable_shot_id=shot.shot_id,
                    global_sequence=shot.global_sequence,
                    segment_sequence=shot_timing.shot_sequence,
                    script_segment_id=script_segment.id,
                    narration_segment_id=narration_segment.id,
                    start_us=shot.start_us,
                    end_us=shot.end_us,
                    global_start_us=shot.global_start_us,
                    global_end_us=shot.global_end_us,
                    usable_duration_us=shot.usable_duration_us,
                    requested_generation_duration_us=shot.requested_generation_duration_us,
                    trim_start_us=shot.trim_start_us,
                    trim_end_us=shot.trim_end_us,
                    transition_handle_us=shot.transition_handle_us,
                    word_start_index=shot.word_start_index,
                    word_end_index=shot.word_end_index,
                    camera=shot.camera.model_dump(mode="json"),
                    action=shot.action.model_dump(mode="json"),
                    transition_in=shot.transition_in.model_dump(mode="json"),
                    transition_out=shot.transition_out.model_dump(mode="json"),
                    references={
                        "character_reference_ids": [
                            str(item) for item in shot.character_reference_ids
                        ],
                        "location_reference_id": str(shot.location_reference_id)
                        if shot.location_reference_id
                        else None,
                        "prop_references": list(shot.prop_references),
                        "evidence_references": [
                            item.model_dump(mode="json") for item in shot.evidence_references
                        ],
                    },
                    incoming_continuity=shot.incoming_continuity.model_dump(mode="json"),
                    outgoing_continuity=shot.expected_outgoing_continuity.model_dump(mode="json"),
                    contract=shot.model_dump(mode="json"),
                    provenance=shot.provenance,
                )
            )
        checkpoint.status = "complete"
        checkpoint.error_code = None
        if isinstance(checkpoint.provider_result, dict):
            checkpoint.provider_result = {**checkpoint.provider_result, "validated": True}
        checkpoint.timing_adjustments = {
            "adjustments": [item.model_dump(mode="json") for item in timing.adjustments],
            "residual_allocation_us": timing.residual_allocation_us,
            "warnings": list(timing.warnings),
        }
        checkpoint.outgoing_continuity = outgoing.model_dump(mode="json")
        self.session.flush()

    def _build_shot(
        self,
        *,
        run: StoryboardRun,
        checkpoint: StoryboardSegmentCheckpoint,
        capability: VisualProviderCapability,
        proposal: StoryboardShotProposal,
        timing: ShotTiming,
        script_segment: ScriptSegment,
        narration_segment: NarrationSegment,
        global_start_us: int,
        global_sequence: int,
    ) -> StoryboardShot:
        # The retimer decides which piece still owns each of the proposal's edges;
        # an interior piece created by a split is joined by a plain cut.
        cut = TransitionPlan(kind="cut")
        return StoryboardShot(
            shot_id=stable_id("shot", checkpoint.input_hash, timing.shot_sequence),
            storyboard_run_id=run.id,
            segment_id=checkpoint.id,
            global_sequence=global_sequence,
            segment_sequence=timing.shot_sequence,
            script_segment_id=script_segment.id,
            narration_segment_id=narration_segment.id,
            start_us=timing.start_us,
            end_us=timing.end_us,
            global_start_us=global_start_us + timing.start_us,
            global_end_us=global_start_us + timing.end_us,
            usable_duration_us=timing.usable_duration_us,
            requested_generation_duration_us=timing.requested_generation_duration_us,
            trim_start_us=timing.trim_start_us,
            trim_end_us=timing.trim_end_us,
            transition_handle_us=timing.transition_handle_us,
            word_start_index=timing.word_start_index,
            word_end_index=timing.word_end_index,
            clause_label=timing.clause_label,
            visual_objective=proposal.visual_objective,
            requires_last_frame=proposal.requires_last_frame,
            camera=proposal.camera,
            action=proposal.action,
            character_reference_ids=list(proposal.character_reference_ids),
            location_reference_id=proposal.location_reference_id,
            prop_references=list(proposal.action.prop_references),
            evidence_references=list(proposal.evidence_references),
            transition_in=proposal.transition_in if timing.carries_lead_edge else cut,
            transition_out=proposal.transition_out if timing.carries_tail_edge else cut,
            incoming_continuity=proposal.incoming_continuity,
            expected_outgoing_continuity=proposal.expected_outgoing_continuity,
            capability_profile_id=capability.capability_profile_id,
            capability_hash=capability.capability_hash,
            warnings=list(proposal.warnings),
            provenance={
                "storyboard_run_id": str(run.id),
                "segment_checkpoint_id": str(checkpoint.id),
                "segment_input_hash": checkpoint.input_hash,
                "proposal_sequence": proposal.proposal_sequence,
                "provider": self.provider.name,
                "model": self.provider.model,
                "director_version": DIRECTOR_VERSION,
                "prompt_version": PROMPT_VERSION,
                "retimer_version": self.retimer_config.version,
                "contract_version": CONTRACT_VERSION,
            },
        )

    def _recover_timing(self, checkpoint: StoryboardSegmentCheckpoint) -> SegmentTiming:
        stored = checkpoint.timing_adjustments or {}
        shots = [
            ShotTiming(
                segment_sequence=checkpoint.sequence,
                shot_sequence=row.segment_sequence,
                proposal_sequence=int(row.provenance.get("proposal_sequence", 0)),
                start_us=row.start_us,
                end_us=row.end_us,
                usable_duration_us=row.usable_duration_us,
                requested_generation_duration_us=row.requested_generation_duration_us,
                trim_start_us=row.trim_start_us,
                trim_end_us=row.trim_end_us,
                transition_handle_us=row.transition_handle_us,
                word_start_index=row.word_start_index,
                word_end_index=row.word_end_index,
                clause_label=str(row.contract.get("clause_label", "")),
                # Edge ownership is a solve-time concept consumed only when shots
                # are built; a recovered segment is never rebuilt, so it is left at
                # its default rather than guessed back out of the persisted row.
            )
            for row in self.repo.segment_shots(checkpoint.id)
        ]
        return SegmentTiming(
            shots=shots,
            adjustments=[
                TimingAdjustment.model_validate(item) for item in stored.get("adjustments", [])
            ],
            residual_allocation_us=int(stored.get("residual_allocation_us", 0)),
            warnings=[str(item) for item in stored.get("warnings", [])],
        )

    # -- finalization ------------------------------------------------------------

    def _finalize_run(
        self,
        *,
        run: StoryboardRun,
        project: Project,
        inputs: AuthoritativeInputs,
        capability: VisualProviderCapability,
        material: dict[str, Any],
        segment_timings: list[tuple[StoryboardSegmentCheckpoint, list[ShotTiming]]],
        adjustments: list[TimingAdjustment],
        residual_total: int,
        total_duration_us: int,
        warnings: list[StructuredNote],
    ) -> StoryboardResult:
        rows = self.repo.shots(run.id)
        by_checkpoint: dict[UUID, list[StoryboardShotRecord]] = {}
        for row in rows:
            by_checkpoint.setdefault(row.segment_checkpoint_id, []).append(row)
        shots: list[StoryboardShot] = []
        segments: list[StoryboardSegment] = []
        boundaries = [0]
        global_sequence = 0
        for checkpoint, _ in segment_timings:
            segment_rows = sorted(
                by_checkpoint.get(checkpoint.id, []), key=lambda row: row.segment_sequence
            )
            for row in segment_rows:
                shot = StoryboardShot.model_validate(row.contract)
                if shot.global_sequence != global_sequence or row.global_sequence != (
                    global_sequence
                ):
                    raise StoryboardLineageError(
                        "shot_sequence_desynchronized",
                        "persisted canonical shot sequences are not dense; rerun the storyboard "
                        "with a fresh idempotency key",
                    )
                shots.append(shot)
                global_sequence += 1
            segments.append(
                StoryboardSegment(
                    segment_id=checkpoint.id,
                    storyboard_run_id=run.id,
                    script_segment_id=checkpoint.script_segment_id,
                    narration_segment_id=checkpoint.narration_segment_id,
                    sequence=checkpoint.sequence,
                    narration_duration_us=checkpoint.narration_duration_us,
                    global_start_us=checkpoint.global_start_us,
                    input_hash=checkpoint.input_hash,
                    shot_count=len(segment_rows),
                    attempt_count=max(1, checkpoint.attempt_count),
                    repair_attempt_count=checkpoint.repair_attempt_count,
                )
            )
            boundaries.append(checkpoint.global_start_us + checkpoint.narration_duration_us)
        self.session.flush()

        diagnostics = validate_storyboard(shots, total_duration_us)
        report = build_report(
            diagnostics,
            checked_segment_sequences=[segment.sequence for segment in segments],
            covered_duration_us=sum(shot.usable_duration_us for shot in shots),
            expected_duration_us=total_duration_us,
        )
        if not report.valid:
            raise StoryboardValidationFailed(report)

        storyboard = Storyboard(
            storyboard_id=stable_id("storyboard", run.id, run.version),
            storyboard_run_id=run.id,
            project_id=run.project_id,
            version=run.version,
            episode_model_id=run.episode_model_id,
            episode_model_hash=str(material["episode_model_hash"]),
            script_id=run.script_id,
            script_version=run.script_version,
            script_hash=str(material["script_hash"]),
            narration_run_id=run.narration_run_id,
            capability_profile_id=capability.capability_profile_id,
            capability_hash=capability.capability_hash,
            contract_version=CONTRACT_VERSION,
            director_version=DIRECTOR_VERSION,
            prompt_version=PROMPT_VERSION,
            retimer_version=self.retimer_config.version,
            input_hash=run.input_hash,
            total_duration_us=total_duration_us,
            segments=segments,
            shots=shots,
            warnings=sorted(warnings, key=lambda note: (note.code, note.message)),
            provenance={
                "pipeline_version": PIPELINE_VERSION,
                "validator_version": VALIDATOR_VERSION,
                "provider": self.provider.name,
                "model": self.provider.model,
                "timing_manifest_id": str(stable_id("timing_manifest", run.id)),
            },
        )
        manifest = TimingManifest(
            storyboard_run_id=run.id,
            project_id=run.project_id,
            script_id=run.script_id,
            script_version=run.script_version,
            narration_run_id=run.narration_run_id,
            capability_profile_id=capability.capability_profile_id,
            capability_hash=capability.capability_hash,
            retimer_version=self.retimer_config.version,
            contract_version=CONTRACT_VERSION,
            segment_boundaries_us=boundaries,
            total_narration_duration_us=total_duration_us,
            total_usable_duration_us=sum(shot.usable_duration_us for shot in shots),
            total_requested_generation_duration_us=sum(
                shot.requested_generation_duration_us for shot in shots
            ),
            total_transition_handle_us=sum(shot.transition_handle_us for shot in shots),
            residual_allocation_us=residual_total,
            entries=[
                TimingManifestEntry(
                    shot_id=shot.shot_id,
                    global_sequence=shot.global_sequence,
                    segment_sequence=shot.segment_sequence,
                    script_segment_id=shot.script_segment_id,
                    narration_segment_id=shot.narration_segment_id,
                    global_start_us=shot.global_start_us,
                    global_end_us=shot.global_end_us,
                    usable_duration_us=shot.usable_duration_us,
                    requested_generation_duration_us=shot.requested_generation_duration_us,
                    trim_start_us=shot.trim_start_us,
                    trim_end_us=shot.trim_end_us,
                    transition_handle_us=shot.transition_handle_us,
                )
                for shot in shots
            ],
            adjustments=adjustments,
            warnings=storyboard.warnings,
        )
        self._store_artifacts(
            run=run, inputs=inputs, storyboard=storyboard, manifest=manifest, report=report
        )
        run.segment_count = len(segments)
        run.shot_count = len(shots)
        run.total_duration_us = total_duration_us
        run.parameters = {
            **material,
            "storyboard_id": str(storyboard.storyboard_id),
            "estimated_cost": str(self._estimated_cost),
            "actual_cost": str(self._actual_cost),
        }
        self.repo.deselect_other_runs(run)
        run.selected = True
        run.status = project.status = "storyboard_complete"
        run.error_code = None
        self.session.commit()
        return self._result(run)

    def _store_artifacts(
        self,
        *,
        run: StoryboardRun,
        inputs: AuthoritativeInputs,
        storyboard: Storyboard,
        manifest: TimingManifest,
        report: StoryboardValidationReport,
    ) -> None:
        parents = self._asset_parents(inputs)
        parameters: dict[str, Any] = {
            "capability_profile_id": run.capability_profile_id,
            "capability_hash": run.capability_hash,
            "contract_version": run.contract_version,
            "director_version": run.director_version,
            "prompt_version": run.prompt_version,
            "retimer_version": run.retimer_version,
            "input_hash": run.input_hash,
            "idempotency_key": run.idempotency_key,
            "episode_model_id": str(run.episode_model_id),
            "script_id": str(run.script_id),
            "script_version": run.script_version,
            "narration_run_id": str(run.narration_run_id),
            "provider_request_ids": sorted(
                checkpoint.provider_request_id
                for checkpoint in self.repo.checkpoints(run.id)
                if checkpoint.provider_request_id
            ),
        }
        # The artifact key is content-bound, not merely run-bound. An identical
        # replay reuses the same key and the same asset, while a run whose canonical
        # output legitimately changed - a targeted repair rebuilt one segment on
        # resume - writes a new artifact instead of colliding on a stale key.
        storyboard_payload = canonical_json(storyboard.model_dump(mode="json")).encode()
        manifest_payload = canonical_json(manifest.model_dump(mode="json")).encode()
        report_payload = canonical_json(report.model_dump(mode="json")).encode()
        storyboard_hash = canonical_hash(storyboard_payload.decode())
        manifest_hash = canonical_hash(manifest_payload.decode())
        report_hash = canonical_hash(report_payload.decode())
        parameters["output_hash"] = storyboard_hash
        storyboard_asset = self.assets.store(
            content=storyboard_payload,
            kind="json",
            media_type=STORYBOARD_MEDIA_TYPE,
            project_id=run.project_id,
            parent_asset_ids=parents,
            provider=run.provider,
            idempotency_key=f"{run.id}:storyboard:{storyboard_hash}",
            generation_parameters=parameters,
        )
        manifest_asset = self.assets.store(
            content=manifest_payload,
            kind="json",
            media_type=TIMING_MANIFEST_MEDIA_TYPE,
            project_id=run.project_id,
            parent_asset_ids=(storyboard_asset.id, *parents),
            provider=run.provider,
            idempotency_key=f"{run.id}:timing-manifest:{manifest_hash}",
            generation_parameters=parameters,
        )
        run.storyboard_asset_id = storyboard_asset.id
        run.timing_manifest_asset_id = manifest_asset.id
        # Stored when it is large, and also whenever it carries diagnostics at all:
        # a run can succeed with warning-severity findings, and those are the whole
        # value of the report. A clean, empty report adds no provenance worth an asset.
        if report.diagnostics or len(report_payload) > LARGE_REPORT_BYTES:
            run.validation_report_asset_id = self.assets.store(
                content=report_payload,
                kind="json",
                media_type=VALIDATION_REPORT_MEDIA_TYPE,
                project_id=run.project_id,
                parent_asset_ids=(storyboard_asset.id,),
                idempotency_key=f"{run.id}:validation-report:{report_hash}",
                generation_parameters=parameters,
            ).id

    def _asset_parents(self, inputs: AuthoritativeInputs) -> tuple[UUID, ...]:
        parents: list[UUID] = [
            inputs.episode_model.canonical_analysis_asset_id,
            inputs.script.canonical_script_asset_id,
        ]
        if inputs.narration_run.preview_asset_id is not None:
            parents.append(inputs.narration_run.preview_asset_id)
        parents.extend(
            segment.normalized_asset_id
            for segment in inputs.narration_segments
            if segment.normalized_asset_id is not None
        )
        seen: list[UUID] = []
        for parent in parents:
            if parent not in seen:
                seen.append(parent)
        return tuple(seen)

    def _result(self, run: StoryboardRun) -> StoryboardResult:
        if run.status not in ("storyboard_complete", "storyboard_failed"):
            raise ValueError("storyboard result requested before a terminal status")
        status: Literal["storyboard_complete", "storyboard_failed"] = (
            "storyboard_complete" if run.status == "storyboard_complete" else "storyboard_failed"
        )
        parameters = run.parameters if isinstance(run.parameters, dict) else {}
        storyboard_id = parameters.get("storyboard_id")
        return StoryboardResult(
            storyboard_run_id=run.id,
            project_id=run.project_id,
            status=status,
            selected=run.selected,
            storyboard_id=UUID(str(storyboard_id)) if storyboard_id else None,
            storyboard_asset_id=run.storyboard_asset_id,
            timing_manifest_asset_id=run.timing_manifest_asset_id,
            validation_report_asset_id=run.validation_report_asset_id,
            segment_count=run.segment_count,
            shot_count=run.shot_count,
            total_duration_us=run.total_duration_us,
            repair_attempt_count=self.repo.repair_attempt_count(run.id),
            provider=run.provider,
            model=run.model,
            estimated_cost=str(parameters.get("estimated_cost", self._estimated_cost)),
            actual_cost=str(parameters.get("actual_cost", self._actual_cost)),
            error_code=run.error_code,
        )
