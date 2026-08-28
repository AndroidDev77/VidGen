"""Restartable T21 repair and fallback routing.

The pipeline consumes one failed T20 result and drives the bounded policy to a
terminal state: a revalidated, selected output, or ``HUMAN_REVIEW_REQUIRED``.

Restart safety is the point. Every attempt checkpoints before it costs anything,
the provider operation name is durable before the first poll, and a repeated
identical request reuses the run, its attempts, its reservations and its QA
results. It creates no second provider request, no second T23 attempt, no second
reservation, no second ledger charge, and no duplicate asset or row.

The pipeline repairs exactly one shot. Sibling shots, their T14/T15 checkpoints
and their passing T20 results are never read for mutation and never touched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from opentelemetry import trace
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from services.animation.downloader import download_video
from services.animation.input_assets import resolve_input_asset
from services.animation.pipeline_errors import AmbiguousVideoSubmission
from services.animation.pricing import estimate_runway_cost
from services.animation.probe import probe_video
from services.animation.providers import CAPABILITIES, VideoGenerationProvider
from services.animation.task_poller import PollingWindowExpired, poll_task
from services.animation.trim import trim_video
from services.animation.validation import validate_video
from services.animation.veo import (
    UnsupportedVeoCapability,
    VeoOperationTimeout,
    VeoRateLimited,
    VeoSubmissionAmbiguous,
    capability_profile,
    estimate_veo_cost,
)
from services.animation.veo_adapter import (
    AlternateVideoProvider,
    VeoInputImage,
    VeoInputImages,
    temporary_download_path,
)
from services.qa.contracts import AuthoritativeInputSelector, AuthoritativeQAInputs
from services.qa.repair_classifier import (
    ClassificationContext,
    ReferenceIntegrity,
    TechnicalSignal,
    classify,
)
from services.qa.repair_planner import (
    DeterministicRepairPlanner,
    PromptRepairRequest,
    RepairPromptPlanner,
    extract_constraints,
    prompt_hash,
    render_prompt,
    validate_delta,
)
from services.qa.repair_policy import RouteContext, default_policy, next_route
from services.renderer.parallax import ParallaxInputs, manifest_bytes, render_parallax
from services.renderer.parallax_manifest import (
    RENDERER_VERSION,
    ParallaxRequest,
    ParallaxSource,
    build_plan,
    decide_eligibility,
)
from vidgen.contracts.animation import (
    RunwayModel,
    VideoProvider,
    VideoProviderRequest,
    VideoTaskStatus,
)
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.repair import (
    HumanReviewReason,
    ParallaxRenderManifest,
    ParallaxRenderResult,
    PromptDelta,
    RepairAttempt,
    RepairAttemptKind,
    RepairAttemptLineage,
    RepairAttemptStatus,
    RepairClassification,
    RepairDecision,
    RepairFailureCategory,
    RepairOutcome,
    RepairPlan,
    RepairPolicy,
    RepairRoute,
    RepairRunState,
    VeoGenerationRequest,
    VeoOperationState,
)
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.contracts.visual_qa import (
    VisualQAOutcome,
    VisualQARepairCode,
    VisualQAResult,
    VisualQATargetType,
)
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.continuity_models import shot_reference_bindings
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import CostRepository
from vidgen.db.image_generation_models import GeneratedKeyframeImage, ImageGenerationItem
from vidgen.db.models import Asset
from vidgen.db.repair_models import (
    RepairAttemptRecord,
    RepairDecisionRecord,
    RepairFallbackRender,
    RepairRun,
    VeoOperationRecord,
)
from vidgen.db.repair_repository import RepairConcurrencyError, RepairRepository
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.visual_qa_models import VisualQAResultRecord, VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

PIPELINE_VERSION = "repair/1.0.0"
REPAIR_OPERATION = "video_repair_generation"


def shot_workflow_identity_resolver_default(session: Session, storyboard: Any, shot: Any) -> str:
    """Reuse the T20/T16 identity helper without importing it at module scope."""
    from services.qa.commands import shot_workflow_identity_resolver

    return shot_workflow_identity_resolver(session, storyboard, shot)


class RepairLineageError(RuntimeError):
    """A deterministic lineage or configuration failure. Nothing is spent."""


class RepairNotRequired(RuntimeError):
    """The shot's authoritative T20 result already passes."""


class RepairCancelled(RuntimeError):
    """Cancellation was requested between paid attempts."""


class Revalidator(Protocol):
    """Run or resume T20 video QA for one shot and return its canonical result."""

    async def __call__(
        self, *, project_id: UUID, shot_id: UUID, idempotency_key: str
    ) -> VisualQAResult: ...


@dataclass(frozen=True, slots=True)
class RepairOptions:
    """Deployment configuration for one repair run."""

    policy: RepairPolicy = field(default_factory=default_policy)
    width: int = 1280
    height: int = 720
    frame_rate: int = 24
    provider_configuration_version: str = "runway/2024-11-06"
    same_provider_model: RunwayModel = RunwayModel.GEN4_TURBO
    alternate_provider_model: str | None = None
    max_polls: int = 20
    poll_interval_seconds: float = 0.0
    #: Veo generates native audio; T17 owns the final mix, so T21 asks for none.
    request_alternate_audio: bool = False


@dataclass(frozen=True, slots=True)
class _Inputs:
    """The authoritative, immutable lineage one repair run is bound to."""

    #: Exactly the inputs T20 proved compatible for this shot's video QA run.
    authoritative: AuthoritativeQAInputs
    image_generation_run_id: UUID
    keyframe: GeneratedKeyframeImage
    keyframe_asset: Asset
    root_video: AnimationGeneratedVideo
    qa_run: VisualQARun
    qa_record: VisualQAResultRecord
    qa_result: VisualQAResult

    @property
    def shot_record(self) -> StoryboardShotRecord:
        return self.authoritative.shot_record

    @property
    def shot(self) -> StoryboardShot:
        return self.authoritative.shot

    @property
    def storyboard(self) -> StoryboardRun:
        return self.authoritative.storyboard


class VisualRepairPipeline:
    """Drive one failed shot through the bounded T21 policy."""

    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        *,
        same_provider: VideoGenerationProvider,
        revalidate: Revalidator,
        alternate_provider: AlternateVideoProvider | None = None,
        planner: RepairPromptPlanner | None = None,
        shot_workflow_identity_resolver: Callable[..., str] | None = None,
        options: RepairOptions | None = None,
        metrics: Metrics | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.same_provider = same_provider
        self.alternate_provider = alternate_provider
        self.revalidate = revalidate
        self.planner = planner or DeterministicRepairPlanner()
        self.identity_resolver = (
            shot_workflow_identity_resolver or shot_workflow_identity_resolver_default
        )
        self.options = options or RepairOptions()
        self.metrics = metrics or Metrics()
        self.cancelled = cancellation_check or (lambda: False)
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)
        self.repository = RepairRepository(session)
        self.qa = VisualQARepository(session)
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.repair")

    # --- public API -------------------------------------------------------
    async def repair(
        self, *, project_id: UUID, shot_id: UUID, idempotency_key: str
    ) -> RepairOutcome:
        """Repair exactly one shot, or explain why it cannot be repaired."""
        # An idempotent replay of a finished repair is free and must not depend
        # on the shot's current QA state: once the repair locked the shot, its
        # authoritative T20 result passes, and re-resolving would report that
        # there is nothing to repair rather than returning the recorded run.
        replay = self.repository.run_by_key(project_id, idempotency_key)
        if replay is not None and self.repository.is_terminal(replay):
            return self._outcome(replay)
        inputs = self._resolve(project_id, shot_id, existing=replay)
        run = self._run(project_id, inputs, idempotency_key)
        if self.repository.is_terminal(run):
            return self._outcome(run)
        current = await self._finish_interrupted_revalidation(run, inputs)
        if self.repository.is_terminal(run):
            return self._outcome(run)
        # A hard ceiling on loop iterations. The policy already bounds paid
        # attempts; this bounds the resume and re-route steps between them, so a
        # pathological state can never spin.
        iterations = 0
        while iterations <= 2 * self.options.policy.max_total_attempts:
            iterations += 1
            try:
                self.repository.claim_advance(run, expected_token=run.advance_token)
            except RepairConcurrencyError:
                self.session.rollback()
                self.session.refresh(run)
                return self._outcome(run)
            decided = self._decide(run, inputs, current)
            if decided is None:
                break
            plan, attempt = decided
            revalidated = await self._execute(run, inputs, plan, attempt)
            if revalidated is None:
                break
            current = revalidated
            if current.outcome is VisualQAOutcome.PASS:
                self._lock(run, attempt, current)
                break
            attempt.status = RepairAttemptStatus.FAILED.value
            attempt.output_qa_result_id = self._result_id(current)
            attempt.completed_at = datetime.now(UTC)
            attempt.failure_category = classify(
                current, context=self._classification_context(run, inputs)
            ).category.value
            self.session.commit()
        return self._outcome(run)

    async def _finish_interrupted_revalidation(
        self, run: RepairRun, inputs: _Inputs
    ) -> VisualQAResult:
        """Revalidate an attempt whose output exists but was never evaluated.

        A worker that died between persisting a generated clip and running T20
        on it has already paid for that clip. Revalidating it is free - T20
        reuses a completed run for the same identity - and it is the only way
        the attempt can be selected, so it happens before any new routing.
        """
        pending = self.repository.latest_attempt(run.id)
        if (
            pending is None
            or pending.status != RepairAttemptStatus.REVALIDATING.value
            or pending.generated_video_id is None
            or pending.output_qa_result_id is not None
        ):
            return inputs.qa_result
        result = await self.revalidate(
            project_id=run.project_id,
            shot_id=inputs.shot_record.id,
            idempotency_key=f"t21-repair:{pending.attempt_identity}",
        )
        if result.outcome is VisualQAOutcome.PASS:
            self._lock(run, pending, result)
            return result
        pending.status = RepairAttemptStatus.FAILED.value
        pending.output_qa_result_id = self._result_id(result)
        pending.completed_at = datetime.now(UTC)
        self.session.commit()
        return result

    # --- authoritative selection -----------------------------------------
    def _resolve(
        self, project_id: UUID, shot_id: UUID, *, existing: RepairRun | None = None
    ) -> _Inputs:
        # T21 reads exactly the inputs T20 already proved compatible. Reusing
        # the T20 selector is what keeps a repair bound to the same selected
        # storyboard, keyframe, clip and approved reference bundle that was
        # evaluated, rather than to a second, subtly different view of them.
        selector = AuthoritativeInputSelector(
            self.session, shot_workflow_identity_resolver=self.identity_resolver
        )
        authoritative = selector.select(project_id, shot_id, VisualQATargetType.VIDEO)
        record = authoritative.shot_record
        keyframe = self.session.scalar(
            select(GeneratedKeyframeImage).where(
                GeneratedKeyframeImage.shot_id == record.id,
                GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
                GeneratedKeyframeImage.selected.is_(True),
            )
        )
        if keyframe is None:
            raise RepairLineageError("shot has no selected T14 keyframe to repair from")
        keyframe_asset = self.session.get(Asset, keyframe.asset_id)
        if keyframe_asset is None:
            raise RepairLineageError("the selected T14 keyframe asset is missing")
        item = self.session.get(ImageGenerationItem, keyframe.item_id)
        if item is None:
            raise RepairLineageError("the selected T14 keyframe has no generation item")
        # A resumed run stays bound to the generation and the QA verdict that
        # started it. Re-resolving "the latest" would drift as the repair itself
        # replaces the shot's selected clip, and the run's identity would no
        # longer match its own idempotency key.
        if existing is not None:
            video = self.session.get(AnimationGeneratedVideo, existing.root_animation_attempt_id)
            qa_record = self.session.get(VisualQAResultRecord, existing.triggering_qa_result_id)
            if video is None or qa_record is None:
                raise RepairLineageError("the repair run's original lineage is missing")
            qa_run = self.session.get(VisualQARun, qa_record.qa_run_id)
            if qa_run is None:
                raise RepairLineageError("the triggering T20 QA run is missing")
            result = self._load_result(qa_run)
        else:
            video = authoritative.video
            if video is None:
                raise RepairLineageError("shot has no selected T15 animation to repair")
            qa_run = self.qa.canonical_run(record.id, VisualQATargetType.VIDEO)
            if qa_run is None:
                raise RepairLineageError("shot has no completed T20 video QA result to repair from")
            found = self.qa.canonical_result(qa_run.id)
            if found is None:
                raise RepairLineageError("the completed T20 QA run has no canonical result")
            qa_record = found
            result = self._load_result(qa_run)
            if result.outcome is VisualQAOutcome.PASS:
                raise RepairNotRequired("the shot's authoritative T20 result already passes")
        return _Inputs(
            authoritative=authoritative,
            image_generation_run_id=item.run_id,
            keyframe=keyframe,
            keyframe_asset=keyframe_asset,
            root_video=video,
            qa_run=qa_run,
            qa_record=qa_record,
            qa_result=result,
        )

    def _load_result(self, qa_run: VisualQARun) -> VisualQAResult:
        """Rebuild the canonical T20 contract from the report T20 already stored.

        T21 reads the persisted report rather than recomputing anything: the
        score, the threshold and the hard-failure flag are T20's to decide.
        """
        if qa_run.report_asset_id is None:
            raise RepairLineageError("the T20 QA run stored no canonical report to classify")
        asset = self.session.get(Asset, qa_run.report_asset_id)
        if asset is None:
            raise RepairLineageError("the T20 QA report asset is missing")
        return VisualQAResult.model_validate_json(self.blob_store.read(asset.storage_key))

    # --- the repair run ---------------------------------------------------
    def _run(self, project_id: UUID, inputs: _Inputs, idempotency_key: str) -> RepairRun:
        material = {
            "project_id": str(project_id),
            "shot_id": str(inputs.shot_record.id),
            "storyboard_run_id": str(inputs.storyboard.id),
            "storyboard_version": inputs.storyboard.version,
            "root_animation_attempt_id": str(inputs.root_video.id),
            "root_video_sha256": inputs.root_video.sha256,
            "triggering_qa_identity": inputs.qa_run.qa_identity,
            "policy_version": self.options.policy.policy_version,
            "planner_version": self.planner.version,
            "same_provider": self.same_provider.name,
            "alternate_provider": (
                self.alternate_provider.name if self.alternate_provider is not None else None
            ),
            "pipeline_version": PIPELINE_VERSION,
        }
        input_hash = _hash(material)
        existing = self.repository.run_by_key(project_id, idempotency_key)
        if existing is not None:
            if existing.input_hash != input_hash:
                raise RepairLineageError(
                    "idempotency key already binds different material repair inputs"
                )
            return existing
        run = RepairRun(
            project_id=project_id,
            shot_id=inputs.shot_record.id,
            root_animation_attempt_id=inputs.root_video.id,
            triggering_qa_result_id=inputs.qa_record.id,
            policy_version=self.options.policy.policy_version,
            policy=self.options.policy.model_dump(mode="json"),
            classifier_version="",
            planner_version=self.planner.version,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            state=RepairRunState.REPAIR_PLANNING.value,
        )
        self.session.add(run)
        self.session.flush()
        self._record_original_attempt(run, inputs)
        self.session.commit()
        return run

    def _record_original_attempt(self, run: RepairRun, inputs: _Inputs) -> None:
        """Record the failed original generation as ordinal 0.

        It is the root of the lineage and it is *not* one of the two repair
        attempts; recording it is what makes "the original does not count"
        checkable rather than merely asserted.
        """
        identity = _hash(
            {
                "repair_run": str(run.id),
                "ordinal": 0,
                "generated_video": str(inputs.root_video.id),
                "sha256": inputs.root_video.sha256,
            }
        )
        self.session.add(
            RepairAttemptRecord(
                repair_run_id=run.id,
                shot_id=inputs.shot_record.id,
                attempt_ordinal=0,
                attempt_kind=RepairAttemptKind.ORIGINAL.value,
                attempt_identity=identity,
                root_animation_attempt_id=inputs.root_video.id,
                predecessor_attempt_id=None,
                provider_attempt_id=inputs.root_video.provider_attempt_id,
                generated_video_id=inputs.root_video.id,
                provider=self.same_provider.name,
                model=self.options.same_provider_model.value,
                provider_operation_id=inputs.root_video.remote_task_id,
                output_asset_ids=[str(inputs.root_video.canonical_asset_id)],
                output_qa_result_id=inputs.qa_record.id,
                status=RepairAttemptStatus.FAILED.value,
                failure_code="visual_qa_failure",
                completed_at=datetime.now(UTC),
            )
        )
        run.total_attempt_count = 1
        self.session.flush()

    # --- routing ----------------------------------------------------------
    def _decide(
        self, run: RepairRun, inputs: _Inputs, result: VisualQAResult
    ) -> tuple[RepairPlan, RepairAttemptRecord] | None:
        """Classify, route, record the decision and materialize the next plan."""
        classification = classify(result, context=self._classification_context(run, inputs))
        run.classification = classification.model_dump(mode="json")
        run.classifier_version = classification.classifier_version
        counts = self.repository.counts(run.id)
        prospective = _prospective_kind(counts, self.options.policy)
        estimate = self._estimate(inputs, prospective)
        allowed, denial, remaining = self._budget_state(run, estimate)
        eligibility = self._eligibility(inputs, result)
        context = RouteContext(
            classification=classification,
            policy=self.options.policy,
            same_provider_repairs_used=counts[RepairAttemptKind.SAME_PROVIDER_REPAIR],
            alternate_provider_attempts_used=counts[RepairAttemptKind.ALTERNATE_PROVIDER],
            fallback_renders_used=counts[RepairAttemptKind.DETERMINISTIC_FALLBACK],
            resumable_operation=self._resumable(run),
            unpersisted_provider_output=self._unpersisted(run),
            ambiguous_submission=self._ambiguous(run),
            alternate_provider_available=self.alternate_provider is not None,
            fallback_eligible=eligibility.eligible,
            fallback_ineligibility_reasons=tuple(eligibility.reasons),
            budget_allows_next_attempt=allowed,
            budget_denial_reason=denial,
            cancellation_requested=bool(run.cancellation_requested or self.cancelled()),
        )
        decision = next_route(context)
        self.repository.record_decision(
            run,
            route=decision.route,
            rationale=decision.rationale,
            planner_version=self.planner.version,
            source_attempt_id=(
                latest.id if (latest := self.repository.latest_attempt(run.id)) else None
            ),
            source_qa_result_id=self._result_id(result),
            classification=classification.model_dump(mode="json"),
            failure_category=classification.category.value,
            repair_codes=[code.value for code in result.repair_codes],
            capability_profile_hash=self._capability_hash(prospective),
            budget_remaining=remaining,
            estimated_next_cost=estimate if decision.consumes_attempt else Decimal("0"),
            human_review_reason=decision.human_review_reason,
        )
        if decision.route is RepairRoute.RESUME_PROVIDER_OPERATION:
            # Resuming finishes work the project has already paid for. It reuses
            # the existing attempt row, its provider attempt, its reservation and
            # its durable operation, so it consumes no bounded attempt.
            resumable = self.repository.latest_attempt(run.id)
            if resumable is None:
                self.repository.mark_state(run, RepairRunState.REPAIR_PLANNING)
                self.session.commit()
                return None
            self.repository.mark_state(run, decision.state)
            self.session.commit()
            return self._plan_from(run, inputs, classification, resumable, Decimal("0")), resumable
        if decision.attempt_kind is None:
            self.repository.mark_state(
                run,
                decision.state,
                human_review_reason=decision.human_review_reason,
            )
            self.session.commit()
            return None
        self.repository.mark_state(run, decision.state)
        plan, attempt = self._plan(run, inputs, classification, decision.attempt_kind, estimate)
        self.session.commit()
        return plan, attempt

    def _classification_context(self, run: RepairRun, inputs: _Inputs) -> ClassificationContext:
        """Everything outside T20 the classifier may consider for this shot."""
        signals: list[TechnicalSignal] = []
        messages: dict[TechnicalSignal, str] = {}
        latest = self.repository.latest_attempt(run.id)
        if latest is not None and latest.failure_code:
            signal = _TECHNICAL_FAILURE_CODES.get(latest.failure_code)
            if signal is not None:
                signals.append(signal)
                messages[signal] = latest.failure_code
        return ClassificationContext(
            reference_integrity=self._reference_integrity(inputs),
            technical_signals=tuple(signals),
            technical_messages=messages,
        )

    def _reference_integrity(self, inputs: _Inputs) -> ReferenceIntegrity:
        """Ask the repository whether the approved references are still valid.

        T21 never mutates an approved T19 reference. It only reports that one is
        missing, stale or incompatible so the correction happens upstream.
        """
        bundle_hash = inputs.qa_result.target.shot_reference_bundle_hash
        current = _current_bundle_hash(self.session, inputs.shot_record.id)
        if current is None:
            return ReferenceIntegrity.MISSING
        if current != bundle_hash:
            return ReferenceIntegrity.STALE
        flagged = any(
            finding.code in _REFERENCE_CONFLICT_CODES
            for dimension in inputs.qa_result.score.dimensions
            for finding in dimension.findings
        )
        return ReferenceIntegrity.CONFLICTING if flagged else ReferenceIntegrity.VALID

    # --- planning ---------------------------------------------------------
    def _plan(
        self,
        run: RepairRun,
        inputs: _Inputs,
        classification: RepairClassification,
        kind: RepairAttemptKind,
        estimate: Decimal,
    ) -> tuple[RepairPlan, RepairAttemptRecord]:
        ordinal = self.repository.next_ordinal(run.id)
        predecessor = self.repository.latest_attempt(run.id)
        provider, model = self._provider_for(kind)
        identity = _hash(
            {
                "repair_run": str(run.id),
                "ordinal": ordinal,
                "kind": kind.value,
                "predecessor": str(predecessor.id) if predecessor is not None else None,
                "shot": str(inputs.shot_record.id),
                "root": str(inputs.root_video.id),
                "classification": classification.primary_code.value,
                "severity": classification.severity.value,
                "provider": provider,
                "model": model,
                "planner": self.planner.version,
                "policy": self.options.policy.policy_version,
                "pipeline": PIPELINE_VERSION,
            }
        )
        existing = self.repository.attempt_by_identity(identity)
        if existing is not None:
            plan = self._plan_from(run, inputs, classification, existing, estimate)
            return plan, existing
        delta = None
        seed = None
        if kind is not RepairAttemptKind.DETERMINISTIC_FALLBACK:
            constraints = extract_constraints(
                inputs.shot, capability_profile=self.options.provider_configuration_version
            )
            base = render_prompt(constraints)
            request = PromptRepairRequest(
                classification=classification,
                constraints=tuple(constraints),
                base_prompt=base,
                base_prompt_hash=prompt_hash(base),
                previous_seed=predecessor.seed if predecessor is not None else None,
                attempt_ordinal=ordinal,
                attempt_identity=identity,
            )
            delta = self.planner.plan(request)
            validate_delta(delta, request)
            seed = delta.new_seed
        attempt = RepairAttemptRecord(
            repair_run_id=run.id,
            shot_id=inputs.shot_record.id,
            attempt_ordinal=ordinal,
            attempt_kind=kind.value,
            attempt_identity=identity,
            root_animation_attempt_id=inputs.root_video.id,
            predecessor_attempt_id=predecessor.id if predecessor is not None else None,
            provider=provider,
            model=model,
            capability_profile_hash=self._capability_hash(kind),
            prompt_hash=delta.after_prompt_hash if delta is not None else None,
            prompt_delta=delta.model_dump(mode="json") if delta is not None else None,
            seed=seed,
            previous_seed=predecessor.seed if predecessor is not None else None,
            reference_asset_ids=[str(item.asset_id) for item in inputs.authoritative.references],
            reference_asset_hashes=[item.sha256 for item in inputs.authoritative.references],
            source_qa_result_id=self._result_id(inputs.qa_result),
            status=RepairAttemptStatus.PLANNED.value,
            estimated_cost=estimate,
            started_at=datetime.now(UTC),
        )
        self.session.add(attempt)
        self.session.flush()
        run.total_attempt_count = len(self.repository.attempts(run.id))
        plan = self._plan_from(run, inputs, classification, attempt, estimate)
        return plan, attempt

    def _plan_from(
        self,
        run: RepairRun,
        inputs: _Inputs,
        classification: RepairClassification,
        attempt: RepairAttemptRecord,
        estimate: Decimal,
    ) -> RepairPlan:
        kind = RepairAttemptKind(attempt.attempt_kind)
        return RepairPlan(
            plan_id=uuid4(),
            repair_run_id=run.id,
            shot_id=inputs.shot_record.id,
            attempt_ordinal=attempt.attempt_ordinal,
            attempt_kind=kind,
            route=_ROUTE_FOR_KIND[kind],
            classification=classification,
            policy=self.options.policy,
            prompt_delta=(
                PromptDelta.model_validate(attempt.prompt_delta)
                if attempt.prompt_delta is not None
                else None
            ),
            repaired_prompt_hash=attempt.prompt_hash,
            provider=attempt.provider,
            model=attempt.model,
            capability_profile_hash=attempt.capability_profile_hash,
            seed=attempt.seed,
            reference_asset_ids=[UUID(value) for value in attempt.reference_asset_ids],
            reference_asset_hashes=list(attempt.reference_asset_hashes),
            estimated_cost=estimate,
            idempotency_key=attempt.attempt_identity,
            planner_version=self.planner.version,
        )

    # --- execution --------------------------------------------------------
    async def _execute(
        self,
        run: RepairRun,
        inputs: _Inputs,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
    ) -> VisualQAResult | None:
        """Produce one candidate output and revalidate it with T20."""
        try:
            if plan.attempt_kind is RepairAttemptKind.DETERMINISTIC_FALLBACK:
                self._render_fallback(run, inputs, plan, attempt)
            elif plan.attempt_kind is RepairAttemptKind.ALTERNATE_PROVIDER:
                await self._generate_alternate(run, inputs, plan, attempt)
            else:
                await self._generate_same_provider(run, inputs, plan, attempt)
        except _AttemptFailed as failure:
            attempt.status = RepairAttemptStatus.FAILED.value
            attempt.failure_code = failure.code[:128]
            attempt.failure_category = failure.category.value
            attempt.completed_at = datetime.now(UTC)
            self.repository.mark_state(run, RepairRunState.REPAIR_PLANNING)
            self.session.commit()
            # A failed attempt is still a spent attempt: the loop re-routes with
            # the same failing T20 result rather than retrying the same route.
            return inputs.qa_result
        attempt.status = RepairAttemptStatus.REVALIDATING.value
        self.repository.mark_state(run, RepairRunState.REVALIDATING)
        self.session.commit()
        # Every repaired or fallback result is revalidated by T20. Nothing is
        # ever selected on the strength of the repair alone.
        return await self.revalidate(
            project_id=run.project_id,
            shot_id=inputs.shot_record.id,
            idempotency_key=f"t21-repair:{attempt.attempt_identity}",
        )

    async def _generate_same_provider(
        self,
        run: RepairRun,
        inputs: _Inputs,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
    ) -> None:
        """One bounded same-provider repair, resuming any durable task."""
        duration = inputs.shot.requested_generation_duration_us / 1_000_000
        capability = CAPABILITIES[self.options.same_provider_model.value]
        prompt = self._repaired_prompt(inputs, plan)
        if len(prompt) > capability.prompt_characters:
            raise _AttemptFailed(
                "unsupported_prompt_length",
                RepairFailureCategory.PROVIDER_ISSUE,
            )
        request = VideoProviderRequest(
            application_idempotency_key=attempt.attempt_identity,
            project_id=run.project_id,
            animation_run_id=run.id,
            animation_item_id=attempt.id,
            storyboard_id=inputs.storyboard.id,
            storyboard_version=inputs.storyboard.version,
            shot_id=inputs.shot.shot_id,
            shot_sequence=inputs.shot_record.global_sequence,
            first_keyframe_asset_id=inputs.keyframe_asset.id,
            first_keyframe_sha256=inputs.keyframe_asset.sha256,
            compiled_motion_prompt=prompt,
            provider=VideoProvider(self.same_provider.name),
            model=self.options.same_provider_model,
            requested_duration_seconds=duration,
            width=self.options.width,
            height=self.options.height,
            seed=plan.seed,
            attempt_number=attempt.attempt_ordinal,
            provider_configuration_version=self.options.provider_configuration_version,
        )
        remote_task_id = attempt.provider_operation_id
        if remote_task_id is None:
            remote_task_id = await self._submit_same_provider(run, plan, attempt, request)
        try:
            task = await poll_task(
                self.same_provider,
                remote_task_id,
                max_polls=self.options.max_polls,
                interval_seconds=self.options.poll_interval_seconds,
                cancellation_check=self.cancelled,
            )
        except PollingWindowExpired:
            # The operation is durable. Nothing is resubmitted and no attempt is
            # consumed: the next invocation resumes this same task.
            attempt.status = RepairAttemptStatus.POLLING.value
            self.session.commit()
            raise
        if task.status is not VideoTaskStatus.SUCCEEDED:
            self._reconcile(attempt, Decimal("0"), billable=False)
            raise _AttemptFailed(
                task.provider_error_code or task.status.value,
                RepairFailureCategory.PROVIDER_ISSUE,
            )
        if len(task.output_handles) != 1:
            raise _AttemptFailed(
                "provider_success_missing_output", RepairFailureCategory.PROVIDER_ISSUE
            )
        attempt.status = RepairAttemptStatus.DOWNLOADING.value
        self.session.commit()
        downloaded = await download_video(task.output_handles[0])
        actual = (
            estimate_runway_cost(self.options.same_provider_model.value, duration)
            if self.same_provider.name == "runway"
            else Decimal("0")
        )
        try:
            self._persist_generated_output(
                run,
                inputs,
                attempt,
                path=downloaded.path,
                provider_request_id=remote_task_id,
                duration=duration,
                actual_cost=actual,
            )
        finally:
            downloaded.path.unlink(missing_ok=True)

    async def _submit_same_provider(
        self,
        run: RepairRun,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
        request: VideoProviderRequest,
    ) -> str:
        keyframe = self.session.get(Asset, request.first_keyframe_asset_id)
        assert keyframe is not None
        resolved = resolve_input_asset(
            self.blob_store,
            keyframe,
            CAPABILITIES[self.options.same_provider_model.value],
            expected_width=self.options.width,
            expected_height=self.options.height,
        )
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=self.same_provider.name,
            model=self.options.same_provider_model.value,
            operation=REPAIR_OPERATION,
            input_hash=attempt.attempt_identity,
            idempotency_key=attempt.attempt_identity,
            related_entity_id=attempt.id,
            attempt_number=attempt.attempt_ordinal,
            estimated_cost=plan.estimated_cost,
        ) as provider_attempt:
            attempt.provider_attempt_id = provider_attempt.row.id
            attempt.status = RepairAttemptStatus.SUBMITTED.value
            self._reserve(run, attempt, plan.estimated_cost)
            self.session.commit()  # durable pre-call checkpoint
            try:
                task = await self.same_provider.submit(request, resolved.data_uri)
            except AmbiguousVideoSubmission:
                attempt.failure_code = "ambiguous_submission"
                self.session.commit()
                raise
            attempt.provider_operation_id = task.remote_task_id
            provider_attempt.set_result(
                provider_request_id=task.provider_request_id or task.remote_task_id
            )
            self.session.commit()  # the remote ID is durable before the first poll
            return task.remote_task_id

    async def _generate_alternate(
        self,
        run: RepairRun,
        inputs: _Inputs,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
    ) -> None:
        """The single bounded Google Veo attempt, driven as a durable operation."""
        provider = self.alternate_provider
        if provider is None:  # pragma: no cover - routing never selects this
            raise _AttemptFailed(
                "alternate_provider_unavailable", RepairFailureCategory.PROVIDER_ISSUE
            )
        profile = provider.capabilities
        canonical_seconds = inputs.shot.usable_duration_us / 1_000_000
        try:
            duration = profile.smallest_supported_duration(canonical_seconds)
            aspect = profile.aspect_ratio_for(self.options.width, self.options.height)
            resolution = cast(
                'Literal["720p", "1080p"]', profile.resolution_for(self.options.height)
            )
        except UnsupportedVeoCapability as error:
            raise _AttemptFailed(
                "unsupported_capability", RepairFailureCategory.PROVIDER_ISSUE
            ) from error
        prompt = self._repaired_prompt(inputs, plan)
        if len(prompt) > profile.max_prompt_characters:
            raise _AttemptFailed("unsupported_prompt_length", RepairFailureCategory.PROVIDER_ISSUE)
        request = VeoGenerationRequest(
            application_idempotency_key=attempt.attempt_identity,
            project_id=run.project_id,
            repair_run_id=run.id,
            repair_attempt_id=attempt.id,
            shot_id=inputs.shot.shot_id,
            attempt_ordinal=attempt.attempt_ordinal,
            model=provider.model,
            capability_profile_version=profile.capability_version,
            capability_profile_hash=profile.profile_hash,
            prompt=prompt,
            prompt_hash=prompt_hash(prompt),
            first_frame_asset_id=inputs.keyframe_asset.id,
            first_frame_sha256=inputs.keyframe_asset.sha256,
            duration_seconds=duration,
            aspect_ratio=aspect,
            resolution=resolution,
            generate_audio=self.options.request_alternate_audio,
            seed=plan.seed,
        )
        checkpoint = self.repository.veo_operation(attempt.id)
        if checkpoint is None:
            checkpoint = await self._submit_alternate(run, plan, attempt, request, provider)
        if checkpoint.submission_ambiguous:
            raise _AttemptFailed("ambiguous_submission", RepairFailureCategory.PROVIDER_ISSUE)
        operation_name = checkpoint.operation_name
        assert operation_name is not None
        result = await self._poll_alternate(provider, checkpoint, operation_name)
        if result.state is not VeoOperationState.SUCCEEDED:
            self._reconcile(attempt, Decimal("0"), billable=False)
            raise _AttemptFailed(
                result.failure_code or "veo_generation_failed",
                RepairFailureCategory.PROVIDER_ISSUE,
            )
        attempt.status = RepairAttemptStatus.DOWNLOADING.value
        self.session.commit()
        destination = temporary_download_path()
        try:
            await provider.download(operation_name, destination)
            self._persist_generated_output(
                run,
                inputs,
                attempt,
                path=destination,
                provider_request_id=operation_name,
                duration=float(duration),
                actual_cost=estimate_veo_cost(provider.model, float(duration)),
                trim_to_canonical=True,
            )
        finally:
            destination.unlink(missing_ok=True)

    async def _submit_alternate(
        self,
        run: RepairRun,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
        request: VeoGenerationRequest,
        provider: AlternateVideoProvider,
    ) -> VeoOperationRecord:
        images = VeoInputImages(
            first_frame=VeoInputImage(
                asset_id_hex=str(request.first_frame_asset_id),
                content=self._asset_bytes(request.first_frame_asset_id),
                media_type="image/png",
            )
            if request.first_frame_asset_id is not None
            else None
        )
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=provider.name,
            model=provider.model,
            operation=REPAIR_OPERATION,
            input_hash=attempt.attempt_identity,
            idempotency_key=attempt.attempt_identity,
            related_entity_id=attempt.id,
            attempt_number=attempt.attempt_ordinal,
            estimated_cost=plan.estimated_cost,
        ) as provider_attempt:
            attempt.provider_attempt_id = provider_attempt.row.id
            attempt.status = RepairAttemptStatus.SUBMITTED.value
            record = VeoOperationRecord(
                repair_attempt_id=attempt.id,
                provider_attempt_id=provider_attempt.row.id,
                application_idempotency_key=attempt.attempt_identity,
                model=provider.model,
                state=VeoOperationState.SUBMITTED.value,
                request_projection={
                    "model": request.model,
                    "duration_seconds": request.duration_seconds,
                    "aspect_ratio": request.aspect_ratio,
                    "resolution": request.resolution,
                    "generate_audio": request.generate_audio,
                    "prompt_hash": request.prompt_hash,
                    "capability_profile_hash": request.capability_profile_hash,
                },
                submitted_at=datetime.now(UTC),
            )
            self.session.add(record)
            self._reserve(run, attempt, plan.estimated_cost)
            self.session.commit()  # durable pre-call checkpoint
            try:
                operation_name = await provider.submit(request, images)
            except VeoSubmissionAmbiguous:
                # Never resubmitted: the operation may already have been billed.
                record.submission_ambiguous = True
                record.failure_code = "ambiguous_submission"
                attempt.failure_code = "ambiguous_submission"
                failure = classify_failure(RuntimeError("ambiguous submission"))
                provider_attempt.row.status = "FAILED"
                provider_attempt.row.failure_class = failure.failure_class
                provider_attempt.row.error_code = "ambiguous_submission"
                self.session.commit()
                return record
            record.operation_name = operation_name
            record.state = VeoOperationState.RUNNING.value
            attempt.provider_operation_id = operation_name
            provider_attempt.set_result(provider_request_id=operation_name[:255])
            self.session.commit()  # the operation name is durable before any poll
            return record

    async def _poll_alternate(
        self,
        provider: AlternateVideoProvider,
        checkpoint: VeoOperationRecord,
        operation_name: str,
    ) -> Any:
        polls = 0
        while polls < self.options.max_polls:
            polls += 1
            try:
                result = await provider.poll(operation_name)
            except VeoRateLimited:
                checkpoint.poll_count += 1
                self.session.commit()
                continue
            checkpoint.poll_count += 1
            checkpoint.last_polled_at = datetime.now(UTC)
            checkpoint.state = result.state.value
            if result.state is not VeoOperationState.RUNNING:
                checkpoint.completed_at = datetime.now(UTC)
                checkpoint.failure_code = result.failure_code
                checkpoint.failure_message = (result.failure_message or "")[:500]
            self.session.commit()
            if result.state is not VeoOperationState.RUNNING:
                return result
        raise VeoOperationTimeout(
            "the Veo polling window expired; the operation stays durable and is resumed"
        )

    # --- deterministic fallback ------------------------------------------
    def _render_fallback(
        self,
        run: RepairRun,
        inputs: _Inputs,
        plan: RepairPlan,
        attempt: RepairAttemptRecord,
    ) -> None:
        """Render the deterministic 2.5D fallback from the approved still."""
        request = self._parallax_request(inputs, attempt.id)
        eligibility = decide_eligibility(request)
        if not eligibility.eligible:
            raise _AttemptFailed("fallback_ineligible", RepairFailureCategory.IMPOSSIBLE_SHOT)
        render_plan = build_plan(request)
        existing = self.repository.fallback_render_by_identity(render_plan.render_identity)
        if existing is not None and existing.repair_attempt_id == attempt.id:
            return
        workspace = TemporaryDirectory(prefix="vidgen-parallax-run-")
        try:
            still = Path(workspace.name) / "source.png"
            still.write_bytes(self._asset_bytes(inputs.keyframe_asset.id))
            rendered = render_parallax(
                render_plan,
                ParallaxInputs(
                    layer_paths=tuple(still for _ in render_plan.layers),
                    asset_ids=(inputs.keyframe_asset.id,),
                    asset_hashes=(inputs.keyframe_asset.sha256,),
                ),
                workspace=Path(workspace.name) / "render",
            )
            manifest_asset = self.assets.store(
                content=manifest_bytes(rendered.manifest),
                kind="parallax_render_manifest",
                media_type="application/json",
                project_id=run.project_id,
                parent_asset_ids=(inputs.keyframe_asset.id,),
                idempotency_key=f"t21-parallax-manifest:{render_plan.render_identity}",
                generation_parameters={"renderer_version": RENDERER_VERSION},
            )
            video = self._persist_generated_output(
                run,
                inputs,
                attempt,
                path=rendered.path,
                untrimmed_path=rendered.untrimmed_path,
                provider_request_id=f"parallax:{render_plan.render_identity[:32]}",
                duration=render_plan.exact_duration_us / 1_000_000,
                actual_cost=Decimal("0"),
                already_canonical=True,
            )
            record = RepairFallbackRender(
                repair_attempt_id=attempt.id,
                shot_id=inputs.shot_record.id,
                render_identity=render_plan.render_identity,
                renderer_version=RENDERER_VERSION,
                input_asset_ids=[str(inputs.keyframe_asset.id)],
                input_asset_hashes=[inputs.keyframe_asset.sha256],
                render_parameters=render_plan.model_dump(mode="json"),
                manifest=rendered.manifest.model_dump(mode="json"),
                exact_duration_us=rendered.measured_duration_us,
                width=rendered.manifest.measured_width,
                height=rendered.manifest.measured_height,
                frame_rate=rendered.frame_rate,
                pixel_format=rendered.pixel_format,
                video_codec=rendered.video_codec,
                ffmpeg_version=rendered.manifest.ffmpeg_version,
                ffprobe_version=rendered.manifest.ffprobe_version,
                ffprobe_metadata=dict(rendered.ffprobe_json),
                output_asset_id=video.canonical_asset_id,
                manifest_asset_id=manifest_asset.id,
                output_sha256=rendered.output_sha256,
            )
            self.session.add(record)
            self.session.commit()
        finally:
            workspace.cleanup()

    def _parallax_request(self, inputs: _Inputs, attempt_id: UUID) -> ParallaxRequest:
        keyframe_run = self.qa.canonical_run(inputs.shot_record.id, VisualQATargetType.KEYFRAME)
        keyframe_result = self.qa.canonical_result(keyframe_run.id) if keyframe_run else None
        codes = tuple(
            code for code in (keyframe_result.repair_codes if keyframe_result is not None else [])
        )
        from vidgen.contracts.visual_qa import VisualQARepairCode

        return ParallaxRequest(
            repair_attempt_id=attempt_id,
            shot_id=inputs.shot_record.id,
            canonical_shot_hash=inputs.qa_result.target.canonical_shot_hash,
            source=ParallaxSource(
                asset_id=inputs.keyframe_asset.id, sha256=inputs.keyframe_asset.sha256
            ),
            width=self.options.width,
            height=self.options.height,
            frame_rate=self.options.frame_rate,
            exact_duration_us=inputs.shot.usable_duration_us,
            required_action=inputs.shot.action.subject_action,
            secondary_action=inputs.shot.action.staging_note or "",
            camera_movement=inputs.shot.camera.movement,
            present_character_count=len(inputs.shot.incoming_continuity.present_character_ids),
            keyframe_repair_codes=tuple(VisualQARepairCode(code) for code in codes),
            keyframe_hard_failure=bool(
                keyframe_result is not None and keyframe_result.hard_failure
            ),
        )

    # --- persistence ------------------------------------------------------
    def _persist_generated_output(
        self,
        run: RepairRun,
        inputs: _Inputs,
        attempt: RepairAttemptRecord,
        *,
        path: Path,
        provider_request_id: str,
        duration: float,
        actual_cost: Decimal,
        untrimmed_path: Path | None = None,
        trim_to_canonical: bool = False,
        already_canonical: bool = False,
    ) -> AnimationGeneratedVideo:
        """Validate, normalize and store one candidate output as a T15 attempt.

        The row is written through the same T15 tables T17 and T20 already read,
        so a repaired shot needs no second selection mechanism. Previous
        attempts stay in place as immutable historical records.
        """
        usable = inputs.shot.usable_duration_us / 1_000_000
        report = validate_video(
            path,
            expected_width=self.options.width,
            expected_height=self.options.height,
            requested_duration=duration,
            minimum_usable_duration=usable,
        )
        if not report.valid or report.probe is None:
            raise _AttemptFailed(
                "technical_validation_failure", RepairFailureCategory.PROVIDER_ISSUE
            )
        original_source = untrimmed_path or path
        original = self.assets.store_file(
            path=original_source,
            kind="repair_original_video",
            media_type="video/mp4",
            project_id=run.project_id,
            parent_asset_ids=(inputs.keyframe_asset.id,),
            provider=attempt.provider or "parallax",
            provider_request_id=provider_request_id[:255],
            idempotency_key=f"t21-original:{attempt.attempt_identity}",
            generation_parameters=self._provenance(attempt, inputs),
            metadata={
                "repair_attempt_id": str(attempt.id),
                "validation": report.model_dump(mode="json"),
            },
        )
        canonical_path = path
        trim_manifest: dict[str, Any] = {}
        trimmed_temporary: Path | None = None
        if not already_canonical and (trim_to_canonical or duration > usable):
            trimmed = trim_video(
                path,
                trim_in_seconds=inputs.shot.trim_start_us / 1_000_000,
                trim_out_seconds=max(0.0, duration - usable),
                usable_duration_seconds=usable,
            )
            canonical_path = trimmed.path
            trimmed_temporary = trimmed.path
            trim_manifest = trimmed.manifest.model_dump(mode="json")
        try:
            canonical_probe = (
                report.probe if canonical_path == path else probe_video(canonical_path)
            )
            canonical = self.assets.store_file(
                path=canonical_path,
                kind="canonical_shot_video",
                media_type="video/mp4",
                project_id=run.project_id,
                parent_asset_ids=(original.id,),
                provider=attempt.provider or "parallax",
                provider_request_id=provider_request_id[:255],
                idempotency_key=f"t21-canonical:{attempt.attempt_identity}",
                generation_parameters=self._provenance(attempt, inputs),
                metadata={
                    "original_asset_id": str(original.id),
                    "trim_manifest": trim_manifest,
                    "repair_attempt_kind": attempt.attempt_kind,
                },
            )
            video = self._write_animation_rows(
                run,
                inputs,
                attempt,
                original.id,
                canonical,
                canonical_probe,
                duration,
                trim_manifest,
                report,
            )
        finally:
            if trimmed_temporary is not None:
                trimmed_temporary.unlink(missing_ok=True)
        attempt.generated_video_id = video.id
        attempt.output_asset_ids = [str(canonical.id)]
        attempt.actual_cost = actual_cost
        self._reconcile(attempt, actual_cost)
        run.total_repair_cost = sum(
            (row.actual_cost for row in self.repository.attempts(run.id)), Decimal("0")
        )
        self.session.commit()
        return video

    def _write_animation_rows(
        self,
        run: RepairRun,
        inputs: _Inputs,
        attempt: RepairAttemptRecord,
        original_asset_id: UUID,
        canonical: Any,
        probe: Any,
        duration: float,
        trim_manifest: dict[str, Any],
        report: Any,
    ) -> AnimationGeneratedVideo:
        storyboard = inputs.storyboard
        animation_run = AnimationRun(
            project_id=run.project_id,
            storyboard_id=storyboard.id,
            storyboard_version=storyboard.version,
            image_generation_run_id=inputs.image_generation_run_id,
            idempotency_key=f"t21-repair:{attempt.attempt_identity}",
            input_hash=attempt.attempt_identity,
            status="animation_complete",
            routing_policy_version=self.options.policy.policy_version,
            provider_configuration_version=self.options.provider_configuration_version[:64],
            pipeline_version=PIPELINE_VERSION,
            requested_item_count=1,
            completed_item_count=1,
            original_video_count=1,
            canonical_video_count=1,
            parameters={
                "repair_run_id": str(run.id),
                "repair_attempt_id": str(attempt.id),
                "attempt_kind": attempt.attempt_kind,
            },
        )
        self.session.add(animation_run)
        self.session.flush()
        item = AnimationItem(
            run_id=animation_run.id,
            shot_id=inputs.shot_record.id,
            shot_sequence=inputs.shot_record.global_sequence,
            first_keyframe_asset_id=inputs.keyframe_asset.id,
            generation_identity=attempt.attempt_identity,
            motion_prompt_hash=attempt.prompt_hash or attempt.attempt_identity,
            motion_prompt_package={
                "repair_attempt_id": str(attempt.id),
                "prompt_hash": attempt.prompt_hash,
                "planner_version": self.planner.version,
            },
            provider=(attempt.provider or "parallax")[:32],
            model=(attempt.model or RENDERER_VERSION)[:32],
            requested_duration=duration,
            width=probe.width,
            height=probe.height,
            status="completed",
            attempt_count=1,
        )
        self.session.add(item)
        self.session.flush()
        self.session.execute(
            update(AnimationGeneratedVideo)
            .where(
                AnimationGeneratedVideo.shot_id == inputs.shot_record.id,
                AnimationGeneratedVideo.selected,
            )
            .values(selected=False)
        )
        video = AnimationGeneratedVideo(
            project_id=run.project_id,
            shot_id=inputs.shot_record.id,
            animation_item_id=item.id,
            provider_attempt_id=attempt.provider_attempt_id
            or inputs.root_video.provider_attempt_id,
            remote_task_id=f"{attempt.attempt_identity[:40]}",
            original_asset_id=original_asset_id,
            canonical_asset_id=canonical.id,
            requested_duration=duration,
            provider_duration=report.probe.duration_seconds,
            canonical_duration=probe.duration_seconds,
            width=probe.width,
            height=probe.height,
            codec=probe.video_codec.value,
            container=probe.container.value,
            frame_rate=probe.frame_rate,
            sha256=canonical.sha256,
            validation_report=report.model_dump(mode="json"),
            trim_manifest=trim_manifest,
            selected=True,
        )
        self.session.add(video)
        self.session.flush()
        item.selected_generated_video_id = video.id
        self.session.flush()
        return video

    # --- selection and cost ----------------------------------------------
    def _lock(self, run: RepairRun, attempt: RepairAttemptRecord, result: VisualQAResult) -> None:
        """Select exactly one revalidated attempt and lock the shot."""
        result_id = self._result_id(result)
        if result_id is None:
            raise RepairLineageError("a passing T20 result must be persisted before selection")
        attempt.output_qa_result_id = result_id
        attempt.completed_at = datetime.now(UTC)
        self.repository.select_attempt(attempt, qa_result_id=result_id)
        run.selected_attempt_id = attempt.id
        run.selected_asset_id = (
            UUID(attempt.output_asset_ids[0]) if attempt.output_asset_ids else None
        )
        run.final_qa_result_id = result_id
        run.final_qa_score = result.score.total
        self.repository.mark_state(run, RepairRunState.LOCKED)
        self.session.commit()

    def _reserve(self, run: RepairRun, attempt: RepairAttemptRecord, estimate: Decimal) -> None:
        """Reserve budget transactionally, reusing an existing reservation."""
        budget = self.session.scalar(
            select(ProjectBudget).where(ProjectBudget.project_id == run.project_id)
        )
        if budget is None or not estimate:
            return
        reservation = self.costs.reserve(
            CostReservationRequest(
                project_id=run.project_id,
                provider_attempt_id=attempt.provider_attempt_id or uuid4(),
                idempotency_key=f"{attempt.attempt_identity}:reservation",
                estimated_amount=estimate,
                currency=budget.currency,
            )
        )
        if reservation.decision in {
            BudgetDecision.DENY_ENTITY_CAP,
            BudgetDecision.DENY_HARD_CAP,
            BudgetDecision.UNKNOWN_PRICE_REVIEW,
        }:
            raise _AttemptFailed("budget_denied", RepairFailureCategory.PROVIDER_ISSUE)
        attempt.reservation_id = reservation.reservation_id

    def _reconcile(
        self, attempt: RepairAttemptRecord, actual: Decimal, *, billable: bool = True
    ) -> None:
        if attempt.reservation_id is None:
            return
        self.costs.reconcile(
            attempt.reservation_id,
            f"{attempt.attempt_identity}:reconciliation",
            actual,
            billable=billable,
        )

    def _budget_state(
        self, run: RepairRun, estimate: Decimal
    ) -> tuple[bool, HumanReviewReason | None, Decimal | None]:
        """Decide, before any provider call, whether the next attempt may run."""
        policy = self.options.policy
        spent = run.total_repair_cost or Decimal("0")
        if policy.per_shot_repair_cost_limit is not None and (
            spent + estimate > policy.per_shot_repair_cost_limit
        ):
            return False, HumanReviewReason.REPAIR_BUDGET_EXHAUSTED, None
        if policy.per_run_repair_cost_limit is not None and (
            spent + estimate > policy.per_run_repair_cost_limit
        ):
            return False, HumanReviewReason.REPAIR_BUDGET_EXHAUSTED, None
        budget = self.session.scalar(
            select(ProjectBudget).where(ProjectBudget.project_id == run.project_id)
        )
        if budget is None:
            return True, None, None
        remaining = budget.hard_cap - budget.committed_amount - budget.reserved_amount
        if estimate > remaining:
            return False, HumanReviewReason.PROJECT_BUDGET_DENIED, max(remaining, Decimal("0"))
        return True, None, remaining

    def _estimate(self, inputs: _Inputs, kind: RepairAttemptKind | None) -> Decimal:
        if kind is RepairAttemptKind.ALTERNATE_PROVIDER and self.alternate_provider is not None:
            profile = self.alternate_provider.capabilities
            try:
                seconds = profile.smallest_supported_duration(
                    inputs.shot.usable_duration_us / 1_000_000
                )
            except UnsupportedVeoCapability:
                return Decimal("0")
            return estimate_veo_cost(self.alternate_provider.model, float(seconds))
        if kind is RepairAttemptKind.SAME_PROVIDER_REPAIR and self.same_provider.name == "runway":
            return estimate_runway_cost(
                self.options.same_provider_model.value,
                inputs.shot.requested_generation_duration_us / 1_000_000,
            )
        return Decimal("0")

    # --- helpers ----------------------------------------------------------
    def _repaired_prompt(self, inputs: _Inputs, plan: RepairPlan) -> str:
        constraints = extract_constraints(
            inputs.shot, capability_profile=self.options.provider_configuration_version
        )
        return render_prompt(constraints, plan.prompt_delta)

    def _asset_bytes(self, asset_id: UUID) -> bytes:
        asset = self.session.get(Asset, asset_id)
        if asset is None:
            raise RepairLineageError("a required input asset is missing")
        return self.blob_store.read(asset.storage_key)

    def _provider_for(self, kind: RepairAttemptKind) -> tuple[str, str]:
        if kind is RepairAttemptKind.ALTERNATE_PROVIDER and self.alternate_provider is not None:
            return self.alternate_provider.name, self.alternate_provider.model
        if kind is RepairAttemptKind.DETERMINISTIC_FALLBACK:
            return "parallax", RENDERER_VERSION
        return self.same_provider.name, self.options.same_provider_model.value

    def _capability_hash(self, kind: RepairAttemptKind | None) -> str | None:
        if kind is RepairAttemptKind.ALTERNATE_PROVIDER:
            profile = (
                self.alternate_provider.capabilities
                if self.alternate_provider is not None
                else capability_profile(self.options.alternate_provider_model)
            )
            return profile.profile_hash
        return None

    def _eligibility(self, inputs: _Inputs, result: VisualQAResult) -> Any:
        del result
        return decide_eligibility(self._parallax_request(inputs, uuid4()))

    def _resumable(self, run: RepairRun) -> bool:
        """A durable provider operation that is still in flight for this run."""
        latest = self.repository.latest_attempt(run.id)
        if latest is None or latest.status not in {
            RepairAttemptStatus.SUBMITTED.value,
            RepairAttemptStatus.POLLING.value,
        }:
            return False
        return bool(latest.provider_operation_id)

    def _unpersisted(self, run: RepairRun) -> bool:
        """A provider operation that succeeded but whose output was never stored.

        Only a *live* attempt qualifies. An attempt that already reached a
        terminal status has been accounted for, and re-reading its operation
        would restart a route the policy has already spent.
        """
        latest = self.repository.latest_attempt(run.id)
        if latest is None or latest.generated_video_id is not None:
            return False
        if latest.status in _TERMINAL_ATTEMPT_STATUSES:
            return False
        checkpoint = self.repository.veo_operation(latest.id)
        return bool(
            checkpoint is not None and checkpoint.state == VeoOperationState.SUCCEEDED.value
        )

    def _ambiguous(self, run: RepairRun) -> bool:
        latest = self.repository.latest_attempt(run.id)
        if latest is not None and latest.failure_code == "ambiguous_submission":
            return True
        checkpoint = self.repository.veo_operation(latest.id) if latest is not None else None
        return bool(checkpoint is not None and checkpoint.submission_ambiguous)

    def _result_id(self, result: VisualQAResult) -> UUID | None:
        run = self.session.get(VisualQARun, result.qa_run_id)
        if run is None:
            return None
        record = self.qa.canonical_result(run.id)
        return record.id if record is not None else None

    def _provenance(self, attempt: RepairAttemptRecord, inputs: _Inputs) -> dict[str, Any]:
        return {
            "repair_run_id": str(attempt.repair_run_id),
            "repair_attempt_id": str(attempt.id),
            "attempt_ordinal": attempt.attempt_ordinal,
            "attempt_kind": attempt.attempt_kind,
            "provider": attempt.provider,
            "model": attempt.model,
            "prompt_hash": attempt.prompt_hash,
            "capability_profile_hash": attempt.capability_profile_hash,
            "seed": attempt.seed,
            "planner_version": self.planner.version,
            "policy_version": self.options.policy.policy_version,
            "pipeline_version": PIPELINE_VERSION,
            "root_animation_attempt_id": str(inputs.root_video.id),
        }

    # --- projection -------------------------------------------------------
    def _outcome(self, run: RepairRun) -> RepairOutcome:
        attempts = [self._attempt_contract(row) for row in self.repository.attempts(run.id)]
        decisions = [self._decision_contract(row) for row in self.repository.decisions(run.id)]
        classification = (
            RepairClassification.model_validate(run.classification) if run.classification else None
        )
        return RepairOutcome(
            repair_run_id=run.id,
            project_id=run.project_id,
            shot_id=run.shot_id,
            root_animation_attempt_id=run.root_animation_attempt_id,
            triggering_qa_result_id=run.triggering_qa_result_id,
            state=RepairRunState(run.state),
            policy=RepairPolicy.model_validate(run.policy),
            classification=classification,
            attempts=attempts,
            decisions=decisions,
            selected_attempt_id=run.selected_attempt_id,
            selected_asset_id=run.selected_asset_id,
            final_qa_result_id=run.final_qa_result_id,
            final_qa_score=float(run.final_qa_score) if run.final_qa_score is not None else None,
            total_attempt_count=len(attempts),
            total_repair_cost=run.total_repair_cost or Decimal("0"),
            currency=run.currency,
            human_review_reason=(
                HumanReviewReason(run.human_review_reason) if run.human_review_reason else None
            ),
            input_hash=run.input_hash,
            idempotency_key=run.idempotency_key,
            created_at=_utc(run.created_at),
            updated_at=_utc(run.updated_at),
        )

    @staticmethod
    def _attempt_contract(row: RepairAttemptRecord) -> RepairAttempt:
        return RepairAttempt(
            attempt_id=row.id,
            repair_run_id=row.repair_run_id,
            lineage=RepairAttemptLineage(
                root_animation_attempt_id=row.root_animation_attempt_id,
                predecessor_attempt_id=row.predecessor_attempt_id,
                shot_id=row.shot_id,
                attempt_ordinal=row.attempt_ordinal,
                attempt_identity=row.attempt_identity,
            ),
            attempt_kind=RepairAttemptKind(row.attempt_kind),
            status=RepairAttemptStatus(row.status),
            provider=row.provider,
            model=row.model,
            provider_attempt_id=row.provider_attempt_id,
            provider_operation_id=row.provider_operation_id,
            prompt_hash=row.prompt_hash,
            prompt_delta=(
                PromptDelta.model_validate(row.prompt_delta)
                if row.prompt_delta is not None
                else None
            ),
            seed=row.seed,
            reference_asset_ids=[UUID(value) for value in row.reference_asset_ids],
            reference_asset_hashes=list(row.reference_asset_hashes),
            capability_profile_hash=row.capability_profile_hash,
            repair_codes=[
                VisualQARepairCode(code)
                for code in (row.prompt_delta or {}).get("repair_codes", [])
            ][:16],
            source_qa_result_id=row.source_qa_result_id,
            output_asset_ids=[UUID(value) for value in row.output_asset_ids],
            output_qa_result_id=row.output_qa_result_id,
            estimated_cost=row.estimated_cost,
            actual_cost=row.actual_cost,
            currency=row.currency,
            failure_category=(
                RepairFailureCategory(row.failure_category) if row.failure_category else None
            ),
            failure_code=row.failure_code,
            trace_context={str(k): str(v) for k, v in (row.trace_context or {}).items()},
            created_at=_utc(row.created_at),
            started_at=_utc(row.started_at) if row.started_at else None,
            completed_at=_utc(row.completed_at) if row.completed_at else None,
        )

    @staticmethod
    def _decision_contract(row: RepairDecisionRecord) -> RepairDecision:
        return RepairDecision(
            decision_id=row.id,
            repair_run_id=row.repair_run_id,
            sequence=row.sequence,
            source_attempt_id=row.source_attempt_id,
            source_qa_result_id=row.source_qa_result_id,
            classification=(
                RepairClassification.model_validate(row.classification)
                if row.classification
                else None
            ),
            repair_codes=[VisualQARepairCode(code) for code in (row.repair_codes or [])][:16],
            route=RepairRoute(row.route),
            rationale=list(row.rationale or [])[:16],
            capability_profile_hash=row.capability_profile_hash,
            budget_remaining=row.budget_remaining,
            estimated_next_cost=row.estimated_next_cost,
            human_review_reason=(
                HumanReviewReason(row.human_review_reason) if row.human_review_reason else None
            ),
            planner_version=row.planner_version,
            policy_version=row.policy_version,
            created_at=_utc(row.created_at),
        )


class _AttemptFailed(RuntimeError):
    """One bounded attempt failed. The router decides what happens next."""

    def __init__(self, code: str, category: RepairFailureCategory) -> None:
        super().__init__(code)
        self.code = code
        self.category = category


#: An attempt in one of these statuses is finished and is never resumed.
_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        RepairAttemptStatus.PASSED.value,
        RepairAttemptStatus.FAILED.value,
        RepairAttemptStatus.CANCELLED.value,
    }
)

_ROUTE_FOR_KIND: dict[RepairAttemptKind, RepairRoute] = {
    RepairAttemptKind.ORIGINAL: RepairRoute.SELECT_PASSING_ATTEMPT,
    RepairAttemptKind.SAME_PROVIDER_REPAIR: RepairRoute.SAME_PROVIDER_REPAIR,
    RepairAttemptKind.ALTERNATE_PROVIDER: RepairRoute.ALTERNATE_PROVIDER,
    RepairAttemptKind.DETERMINISTIC_FALLBACK: RepairRoute.DETERMINISTIC_FALLBACK,
}

_TECHNICAL_FAILURE_CODES: dict[str, TechnicalSignal] = {
    "veo_safety_filtered": TechnicalSignal.PROVIDER_SAFETY_REJECTION,
    "unsupported_capability": TechnicalSignal.UNSUPPORTED_CAPABILITY,
    "unsupported_prompt_length": TechnicalSignal.UNSUPPORTED_CAPABILITY,
    "technical_validation_failure": TechnicalSignal.CORRUPT_DOWNLOAD,
    "veo_incomplete_download": TechnicalSignal.CORRUPT_DOWNLOAD,
    "veo_corrupt_output": TechnicalSignal.CORRUPT_DOWNLOAD,
}

_REFERENCE_CONFLICT_CODES = frozenset(
    {
        "reference_conflict",
        "reference_mismatch",
        "incompatible_reference",
        "missing_reference",
        "stale_reference",
        "reference_version_conflict",
    }
)


def _prospective_kind(
    counts: dict[RepairAttemptKind, int], policy: RepairPolicy
) -> RepairAttemptKind | None:
    """The attempt kind the policy would choose next, ignoring budget."""
    if counts[RepairAttemptKind.SAME_PROVIDER_REPAIR] < policy.max_same_provider_repairs:
        return RepairAttemptKind.SAME_PROVIDER_REPAIR
    if counts[RepairAttemptKind.ALTERNATE_PROVIDER] < policy.max_alternate_provider_attempts:
        return RepairAttemptKind.ALTERNATE_PROVIDER
    if counts[RepairAttemptKind.DETERMINISTIC_FALLBACK] < policy.max_fallback_renders:
        return RepairAttemptKind.DETERMINISTIC_FALLBACK
    return None


def _current_bundle_hash(session: Session, shot_id: UUID) -> str | None:
    row = session.execute(
        select(shot_reference_bindings.c.bundle_hash)
        .where(shot_reference_bindings.c.storyboard_shot_id == shot_id)
        .where(shot_reference_bindings.c.status == "approved")
        .order_by(shot_reference_bindings.c.created_at.desc())
        .limit(1)
    ).first()
    return str(row[0]) if row is not None else None


def _probe(path: Path) -> Any:

    return probe_video(path)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def parallax_result_contract(
    record: RepairFallbackRender,
) -> ParallaxRenderResult:
    """Project one persisted fallback render into its public contract."""
    manifest = ParallaxRenderManifest.model_validate(record.manifest)
    return ParallaxRenderResult(
        repair_attempt_id=record.repair_attempt_id,
        render_identity=record.render_identity,
        manifest=manifest,
        output_asset_id=record.output_asset_id,
        manifest_asset_id=record.manifest_asset_id,
        output_sha256=record.output_sha256,
        exact_duration_us=manifest.plan.exact_duration_us,
        width=record.width,
        height=record.height,
        frame_rate=record.frame_rate,
        pixel_format=record.pixel_format,
        video_codec=record.video_codec,
        qa_result_id=record.qa_result_id,
    )


__all__ = [
    "PIPELINE_VERSION",
    "REPAIR_OPERATION",
    "RepairCancelled",
    "RepairLineageError",
    "RepairNotRequired",
    "RepairOptions",
    "Revalidator",
    "VisualRepairPipeline",
    "parallax_result_contract",
]
