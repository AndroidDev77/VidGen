"""Restartable T15 pipeline over authoritative selected T13/T14 inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from services.animation.downloader import download_video
from services.animation.input_assets import resolve_input_asset
from services.animation.motion_prompt import compile_motion_prompt
from services.animation.pipeline_errors import AmbiguousVideoSubmission
from services.animation.pricing import estimate_runway_cost
from services.animation.providers import CAPABILITIES, VideoGenerationProvider, validate_request
from services.animation.routing import ROUTING_POLICY_VERSION, RoutingContext, route_model
from services.animation.task_poller import PollingWindowExpired, poll_task
from services.animation.trim import trim_video
from services.animation.validation import validate_video
from vidgen.contracts.animation import (
    AnimationResult,
    GeneratedVideoCandidate,
    MotionIntent,
    RunwayModel,
    ShotAnimationResult,
    VideoProvider,
    VideoProviderRequest,
    VideoTaskStatus,
)
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.db.animation_models import (
    AnimationGeneratedVideo,
    AnimationItem,
    AnimationRun,
    RunwayTask,
)
from vidgen.db.animation_repository import AnimationInputs, AnimationRepository
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.db.models import Project
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

PIPELINE_VERSION = "animation/1.0.0"
VALIDATION_VERSION = "technical-video/1.0"


class AnimationCancelled(RuntimeError):
    pass


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class AnimationPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: VideoGenerationProvider,
        *,
        requested_model: RunwayModel | None = None,
        width: int = 1280,
        height: int = 720,
        provider_configuration_version: str = "runway/2024-11-06",
        cancellation_check: Callable[[], bool] | None = None,
        metrics: Metrics | None = None,
        max_polls: int = 20,
        poll_interval_seconds: float = 1,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.requested_model = requested_model
        self.width = width
        self.height = height
        self.provider_configuration_version = provider_configuration_version
        self.cancelled = cancellation_check or (lambda: False)
        self.metrics = metrics or Metrics()
        self.max_polls = max_polls
        self.poll_interval_seconds = poll_interval_seconds
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.animation")
        self.repo = AnimationRepository(session)
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)

    async def process(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        storyboard_id: UUID | None = None,
        image_run_id: UUID | None = None,
        shot_id: UUID | None = None,
    ) -> AnimationResult:
        inputs = self.repo.authoritative_inputs(
            project_id, storyboard_id=storyboard_id, image_run_id=image_run_id, shot_id=shot_id
        )
        material = {
            "project": project_id,
            "storyboard": inputs.storyboard.storyboard.id,
            "storyboard_version": inputs.storyboard.storyboard.version,
            "storyboard_hash": inputs.storyboard.storyboard_asset.sha256,
            "timing_hash": inputs.storyboard.timing_asset.sha256,
            "image_run": inputs.image_run.id,
            "provider": self.provider.name,
            "requested_model": self.requested_model,
            "dimensions": [self.width, self.height],
            "routing": ROUTING_POLICY_VERSION,
            "provider_configuration": self.provider_configuration_version,
            "pipeline": PIPELINE_VERSION,
            "validation": VALIDATION_VERSION,
            "shot": shot_id,
        }
        input_hash = _hash(material)
        run = self.repo.run_by_key(project_id, idempotency_key)
        if run is not None and run.input_hash != input_hash:
            raise ValueError("idempotency key already binds different material animation inputs")
        if run is None:
            run = AnimationRun(
                project_id=project_id,
                storyboard_id=inputs.storyboard.storyboard.id,
                storyboard_version=inputs.storyboard.storyboard.version,
                image_generation_run_id=inputs.image_run.id,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                status="animation_queued",
                routing_policy_version=ROUTING_POLICY_VERSION,
                provider_configuration_version=self.provider_configuration_version,
                pipeline_version=PIPELINE_VERSION,
                parameters=json.loads(json.dumps(material, default=str)),
            )
            self.session.add(run)
            self.session.flush()
            self.session.commit()
        targets = [
            row for row in inputs.storyboard.shots if shot_id in {None, row.id, row.stable_shot_id}
        ]
        if shot_id is not None and not targets:
            raise ValueError("requested shot is not part of the authoritative selected storyboard")
        run.requested_item_count = len(targets)
        results: list[ShotAnimationResult] = []
        try:
            for row in targets:
                if self.cancelled():
                    raise AnimationCancelled("T15 cancellation requested before submission")
                results.append(await self._process_item(inputs, run, row))
            run.completed_item_count = sum(
                item.status in {"completed", "reused"} for item in results
            )
            run.failed_item_count = sum(item.status == "failed" for item in results)
            run.original_video_count = run.completed_item_count
            run.canonical_video_count = run.completed_item_count
            complete = (
                run.completed_item_count == run.requested_item_count and not run.failed_item_count
            )
            run.status = "animation_complete" if complete else "animation_failed"
            inputs.storyboard.project.status = run.status
            self.session.commit()
        except PollingWindowExpired:
            self.session.rollback()
            durable = self.session.get(AnimationRun, run.id)
            project = self.session.get(Project, project_id)
            if durable is not None:
                durable.status = "animation_polling"
            if project is not None:
                project.status = "animation_polling"
            self.session.commit()
            raise
        except (AnimationCancelled, asyncio.CancelledError):
            self.session.rollback()
            raise
        except BaseException as error:
            self.session.rollback()
            durable = self.session.get(AnimationRun, run.id)
            project = self.session.get(Project, project_id)
            if durable is not None:
                durable.status = "animation_failed"
                durable.error_code = type(error).__name__[:128]
            if project is not None:
                project.status = "animation_failed"
            self.session.commit()
            raise
        return AnimationResult(
            run_id=run.id,
            storyboard_id=run.storyboard_id,
            image_generation_run_id=run.image_generation_run_id,
            requested_count=run.requested_item_count,
            submitted_count=sum(item.status == "completed" for item in results),
            polling_count=0,
            completed_count=sum(item.status == "completed" for item in results),
            reused_count=sum(item.status == "reused" for item in results),
            failed_count=run.failed_item_count,
            status=run.status,
            items=results,
        )

    async def _process_item(
        self, inputs: AnimationInputs, run: AnimationRun, row: Any
    ) -> ShotAnimationResult:
        shot = StoryboardShot.model_validate(row.contract)
        frame = inputs.keyframes[row.id]
        intent = self._motion_intent(shot, inputs.storyboard.project.visual_style)
        package = compile_motion_prompt(intent)
        hero = float(shot.provenance.get("importance", 0)) >= 0.8
        settings = inputs.storyboard.project.settings
        premium_permitted = bool(settings.get("quality_profile") in {"premium", "hero"})
        premium_budget = self._premium_budget_available(inputs.storyboard.project.id, shot)
        model = route_model(
            RoutingContext(hero, premium_permitted, premium_budget), self.requested_model
        )
        capability = CAPABILITIES[model.value]
        # Snap to the nearest supported whole-second duration. Storyboards created
        # before the retimer enforced integer durations may carry fractional values.
        raw_duration = shot.requested_generation_duration_us / 1_000_000
        duration = next(
            (d for d in sorted(capability.durations) if d >= raw_duration),
            max(capability.durations),
        )
        strict_last = bool(shot.requires_last_frame)
        warnings: list[dict[str, str]] = []
        last_asset_id = frame.last_asset.id if frame.last_asset else None
        last_hash = frame.last_asset.sha256 if frame.last_asset else None
        if frame.last is not None and not capability.supports_last_frame:
            warnings.append(
                {
                    "code": "last_frame_not_sent",
                    "message": f"{model.value} has no last-frame control",
                }
            )
            if strict_last:
                warnings.append(
                    {
                        "code": "strict_last_frame_downgraded",
                        "message": (
                            f"{model.value} does not support last-frame control; "
                            "proceeding without last-frame anchor"
                        ),
                    }
                )
            last_asset_id = None
            last_hash = None
        identity = _hash(
            {
                "project": run.project_id,
                "storyboard": run.storyboard_id,
                "storyboard_version": run.storyboard_version,
                "timing_hash": inputs.storyboard.timing_asset.sha256,
                "shot": row.stable_shot_id,
                "sequence": row.global_sequence,
                "shot_hash": _hash(row.contract),
                "first_asset": frame.first_asset.id,
                "first_hash": frame.first_asset.sha256,
                "last_asset": last_asset_id,
                "last_hash": last_hash,
                "prompt_hash": package.prompt_hash,
                "provider": self.provider.name,
                "model": model.value,
                "duration": duration,
                "dimensions": [self.width, self.height],
                "format": "mp4",
                "capability_hash": shot.capability_hash,
                "routing": ROUTING_POLICY_VERSION,
                "provider_configuration": self.provider_configuration_version,
                "pipeline": PIPELINE_VERSION,
                "validation": VALIDATION_VERSION,
                "trim": [shot.trim_start_us, shot.trim_end_us, shot.usable_duration_us],
            }
        )
        item = self.repo.item_by_identity(identity)
        if item is not None:
            video = self.repo.video_for_item(item.id)
            if item.status == "completed" and video is not None:
                return self._result(item, video, "reused", shot.shot_id)
            if item.run_id != run.id:
                raise ValueError("animation identity belongs to an incompatible run")
            if item.status == "provider_outcome_ambiguous":
                raise AmbiguousVideoSubmission(
                    "ambiguous submission requires manual reconciliation"
                )
        else:
            # Fallback: an item for this (run, shot) slot may already exist with a
            # different identity if the pipeline configuration changed between attempts
            # (e.g. duration rounding introduced after the original attempt committed).
            item = self.session.scalar(
                select(AnimationItem).where(
                    AnimationItem.run_id == run.id,
                    AnimationItem.shot_id == row.id,
                )
            )
            if item is not None:
                # Migrate the item's identity to the current one so all downstream
                # operations (ProviderAttempt, cost accounting) use the new idempotency
                # keys. The old identity slot is now free; the new identity is unique
                # because item_by_identity(identity) returned None above.
                item.generation_identity = identity
                self.session.flush()
                self.session.commit()
            else:
                item = AnimationItem(
                    run_id=run.id,
                    shot_id=row.id,
                    shot_sequence=row.global_sequence,
                    first_keyframe_asset_id=frame.first_asset.id,
                    last_keyframe_asset_id=last_asset_id,
                    generation_identity=identity,
                    motion_prompt_hash=package.prompt_hash,
                    motion_prompt_package=package.model_dump(mode="json"),
                    provider=self.provider.name,
                    model=model.value,
                    requested_duration=duration,
                    width=self.width,
                    height=self.height,
                    status="animation_queued",
                    warnings=warnings,
                )
                self.session.add(item)
                self.session.flush()
                self.session.commit()
        request = VideoProviderRequest(
            application_idempotency_key=identity,
            project_id=run.project_id,
            animation_run_id=run.id,
            animation_item_id=item.id,
            storyboard_id=run.storyboard_id,
            storyboard_version=run.storyboard_version,
            shot_id=shot.shot_id,
            shot_sequence=row.global_sequence,
            first_keyframe_asset_id=frame.first_asset.id,
            first_keyframe_sha256=frame.first_asset.sha256,
            last_keyframe_asset_id=last_asset_id,
            last_keyframe_sha256=last_hash,
            compiled_motion_prompt=package.prompt,
            provider=VideoProvider(self.provider.name),
            model=model,
            requested_duration_seconds=duration,
            width=self.width,
            height=self.height,
            attempt_number=max(1, item.attempt_count + 1),
            provider_configuration_version=self.provider_configuration_version,
        )
        validate_request(request)
        resolved = resolve_input_asset(
            self.blob_store,
            frame.first_asset,
            capability,
            expected_width=self.width,
            expected_height=self.height,
        )
        task = self.repo.task_for_item(item.id)
        if task is None or task.provider_status in {"failed", "submission_failed"}:
            task = await self._submit(inputs, run, item, request, resolved.data_uri)
        if task.remote_task_id is None:
            if task.provider_status == "ambiguous":
                raise AmbiguousVideoSubmission("submission checkpoint has no remote task ID")
            raise ValueError(
                "known submission failure has no remote task: "
                f"{task.failure_code or task.provider_status}"
            )
        run.status = inputs.storyboard.project.status = "animation_polling"
        item.status = "animation_polling"
        self.session.commit()

        async def checkpoint(provider_task: Any) -> None:
            task.provider_status = provider_task.status.value
            task.last_polled_at = provider_task.last_polled_at
            task.poll_count += 1
            task.progress = provider_task.progress
            task.failure_code = provider_task.provider_error_code
            task.failure_message = provider_task.failure_reason
            if provider_task.status in {
                VideoTaskStatus.SUCCEEDED,
                VideoTaskStatus.FAILED,
                VideoTaskStatus.CANCELLED,
            }:
                task.terminal_at = provider_task.completed_at
            self.session.commit()

        try:
            provider_task = await poll_task(
                self.provider,
                task.remote_task_id,
                max_polls=self.max_polls,
                interval_seconds=self.poll_interval_seconds,
                checkpoint=checkpoint,
                cancellation_check=self.cancelled,
            )
        except BaseException as error:
            if isinstance(error, (AnimationCancelled, asyncio.CancelledError)):
                cancelled = await self.provider.cancel(task.remote_task_id)
                task.cancellation_status = "provider_cancelled" if cancelled else "not_cancelled"
                self.session.commit()
            raise
        if provider_task.status != VideoTaskStatus.SUCCEEDED:
            item.status = "animation_failed"
            item.error_code = provider_task.provider_error_code or provider_task.status.value
            self._reconcile(task, identity, Decimal("0"), billable=False)
            self.session.commit()
            return ShotAnimationResult(
                shot_id=shot.shot_id,
                status="failed",
                remote_task_id=task.remote_task_id,
                error_code=item.error_code,
            )
        if len(provider_task.output_handles) != 1:
            raise ValueError("provider_success_missing_output: exactly one primary output required")
        run.status = inputs.storyboard.project.status = "animation_downloading"
        item.status = "animation_downloading"
        self.session.commit()
        downloaded = await download_video(provider_task.output_handles[0])
        trimmed_path: Path | None = None
        try:
            report = validate_video(
                downloaded.path,
                expected_width=self.width,
                expected_height=self.height,
                requested_duration=duration,
                minimum_usable_duration=shot.usable_duration_us / 1_000_000,
            )
            if not report.valid or report.probe is None:
                raise ValueError("provider output failed deterministic technical validation")
            original = self.assets.store_file(
                path=downloaded.path,
                kind="runway_original_video",
                media_type="video/mp4",
                project_id=run.project_id,
                parent_asset_ids=(
                    inputs.storyboard.storyboard_asset.id,
                    inputs.storyboard.timing_asset.id,
                    frame.first_asset.id,
                ),
                provider=self.provider.name,
                provider_request_id=task.remote_task_id,
                idempotency_key=f"animation-original:{identity}",
                generation_parameters=self._generation_parameters(
                    request, shot, package.prompt_hash
                ),
                metadata={
                    "animation_item_id": str(item.id),
                    "provider_attempt_id": str(task.provider_attempt_id),
                    "download_sha256": downloaded.sha256,
                    "validation": report.model_dump(mode="json"),
                },
            )
            run.status = inputs.storyboard.project.status = "animation_trimming"
            item.status = "animation_trimming"
            self.session.commit()
            trimmed = trim_video(
                downloaded.path,
                trim_in_seconds=shot.trim_start_us / 1_000_000,
                trim_out_seconds=shot.trim_end_us / 1_000_000,
                usable_duration_seconds=shot.usable_duration_us / 1_000_000,
            )
            trimmed_path = trimmed.path
            canonical = self.assets.store_file(
                path=trimmed.path,
                kind="canonical_shot_video",
                media_type="video/mp4",
                project_id=run.project_id,
                parent_asset_ids=(original.id,),
                provider=self.provider.name,
                provider_request_id=task.remote_task_id,
                idempotency_key=f"animation-canonical:{identity}",
                generation_parameters={
                    "ffmpeg_arguments": trimmed.manifest.ffmpeg_arguments,
                    "ffmpeg_version": trimmed.ffmpeg_version,
                    "encoding_profile": trimmed.manifest.encoding_profile,
                },
                metadata={
                    "original_asset_id": str(original.id),
                    "input_hash": trimmed.input_sha256,
                    "canonical_hash": trimmed.output_sha256,
                    "measured_usable_duration": trimmed.probe.duration_seconds,
                    "trim_manifest": trimmed.manifest.model_dump(mode="json"),
                },
            )
            self.session.execute(
                update(AnimationGeneratedVideo)
                .where(
                    AnimationGeneratedVideo.shot_id == row.id,
                    AnimationGeneratedVideo.selected,
                )
                .values(selected=False)
            )
            video = AnimationGeneratedVideo(
                project_id=run.project_id,
                shot_id=row.id,
                animation_item_id=item.id,
                provider_attempt_id=task.provider_attempt_id,
                remote_task_id=task.remote_task_id,
                original_asset_id=original.id,
                canonical_asset_id=canonical.id,
                requested_duration=duration,
                provider_duration=report.probe.duration_seconds,
                canonical_duration=trimmed.probe.duration_seconds,
                width=trimmed.probe.width,
                height=trimmed.probe.height,
                codec=trimmed.probe.video_codec,
                container=trimmed.probe.container,
                frame_rate=trimmed.probe.frame_rate,
                sha256=canonical.sha256,
                validation_report=report.model_dump(mode="json"),
                trim_manifest=trimmed.manifest.model_dump(mode="json"),
                selected=True,
            )
            self.session.add(video)
            self.session.flush()
            item.selected_generated_video_id = video.id
            item.status = "completed"
            actual = (
                estimate_runway_cost(model.value, duration)
                if self.provider.name == "runway"
                else Decimal("0")
            )
            self._reconcile(task, identity, actual)
            self.session.commit()
            return self._result(item, video, "completed", shot.shot_id)
        finally:
            downloaded.path.unlink(missing_ok=True)
            if trimmed_path is not None:
                trimmed_path.unlink(missing_ok=True)

    async def _submit(
        self,
        inputs: AnimationInputs,
        run: AnimationRun,
        item: AnimationItem,
        request: VideoProviderRequest,
        prompt_image: str,
    ) -> RunwayTask:
        estimated = (
            estimate_runway_cost(request.model.value, request.requested_duration_seconds)
            if self.provider.name == "runway"
            else Decimal("0")
        )
        # Include the current attempt_count so each resubmission (e.g. after a
        # failed Runway task) gets its own ProviderAttempt and cost reservation.
        # This prevents uq_runway_task_item_attempt violations on retry.
        attempt_key = f"{request.application_idempotency_key}:{item.attempt_count}"
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=self.provider.name,
            model=request.model.value,
            operation="video_generation",
            input_hash=request.application_idempotency_key,
            idempotency_key=attempt_key,
            related_entity_id=item.id,
            attempt_number=request.attempt_number,
            estimated_cost=estimated,
        ) as attempt:
            task = RunwayTask(
                animation_item_id=item.id,
                provider_attempt_id=attempt.row.id,
                provider_status="pre_call_checkpoint",
                request_projection={
                    "project_id": str(request.project_id),
                    "animation_item_id": str(request.animation_item_id),
                    "first_keyframe_asset_id": str(request.first_keyframe_asset_id),
                    "first_keyframe_sha256": request.first_keyframe_sha256,
                    "model": request.model.value,
                    "duration": request.requested_duration_seconds,
                    "ratio": f"{request.width}:{request.height}",
                    "prompt_hash": item.motion_prompt_hash,
                },
            )
            self.session.add(task)
            item.status = "animation_submitting"
            item.attempt_count += 1
            run.status = inputs.storyboard.project.status = "animation_submitting"
            self.session.flush()
            reservation_id = None
            has_budget = self.session.scalar(
                select(ProjectBudget.id).where(ProjectBudget.project_id == run.project_id)
            )
            if self.provider.name == "runway" and has_budget is not None:
                reservation = self.costs.reserve(
                    CostReservationRequest(
                        project_id=run.project_id,
                        provider_attempt_id=attempt.row.id,
                        idempotency_key=f"{attempt_key}:reservation",
                        estimated_amount=estimated,
                        currency="USD",
                    )
                )
                if reservation.decision in {
                    BudgetDecision.DENY_ENTITY_CAP,
                    BudgetDecision.DENY_HARD_CAP,
                    BudgetDecision.UNKNOWN_PRICE_REVIEW,
                }:
                    raise BudgetExceededError(f"Runway generation denied: {reservation.decision}")
                reservation_id = reservation.reservation_id
            task.response_metadata = {
                "reservation_id": str(reservation_id) if reservation_id else None,
                "estimated_cost": str(estimated),
                "attempt_key": attempt_key,
            }
            self.session.commit()  # durable pre-call checkpoint
            try:
                provider_task = await self.provider.submit(request, prompt_image)
            except AmbiguousVideoSubmission:
                item.status = "provider_outcome_ambiguous"
                task.provider_status = "ambiguous"
                failure = classify_failure(
                    AmbiguousVideoSubmission("submission response did not contain a task ID")
                )
                attempt.row.status = "FAILED"
                attempt.row.failure_class = failure.failure_class
                attempt.row.error_code = failure.error_code
                attempt.row.retryable = failure.retryable
                self.session.commit()
                raise
            except BaseException as error:
                failure = classify_failure(error, status_code=getattr(error, "status_code", None))
                item.status = "animation_failed"
                item.error_code = failure.error_code
                task.provider_status = "submission_failed"
                task.failure_code = failure.error_code
                _body = getattr(error, "body", None)
                _provider_msg = _body.get("error") if isinstance(_body, dict) else None
                task.failure_message = (_provider_msg or failure.sanitized_message)[:1024]
                self.session.commit()
                raise
            task.remote_task_id = provider_task.remote_task_id
            task.provider_status = provider_task.status.value
            attempt.set_result(
                provider_request_id=provider_task.provider_request_id
                or provider_task.remote_task_id
            )
            self.session.commit()  # remote ID is durable before the first poll
            return task

    def _reconcile(
        self, task: RunwayTask, identity: str, actual: Decimal, *, billable: bool = True
    ) -> None:
        reservation = task.response_metadata.get("reservation_id")
        if reservation:
            # Prefer the attempt-specific key stored on the task; fall back to the
            # legacy identity-only key for tasks created before this change.
            base_key = task.response_metadata.get("attempt_key", identity)
            self.costs.reconcile(
                UUID(str(reservation)), f"{base_key}:reconciliation", actual, billable=billable
            )

    def _premium_budget_available(self, project_id: UUID, shot: StoryboardShot) -> bool:
        if self.provider.name != "runway":
            return True
        budget = self.session.scalar(
            select(ProjectBudget).where(ProjectBudget.project_id == project_id)
        )
        if budget is None:
            return False
        duration = shot.requested_generation_duration_us / 1_000_000
        remaining = budget.hard_cap - budget.committed_amount - budget.reserved_amount
        return remaining >= estimate_runway_cost("gen4.5", duration)

    @staticmethod
    def _motion_intent(shot: StoryboardShot, visual_style: str = "") -> MotionIntent:
        incoming = shot.incoming_continuity
        outgoing = shot.expected_outgoing_continuity
        return MotionIntent(
            shot_id=shot.shot_id,
            shot_sequence=shot.global_sequence,
            visual_purpose=shot.visual_objective,
            primary_action=shot.action.subject_action,
            start_pose=str(
                shot.provenance.get("start_pose") or shot.action.staging_note or "held pose"
            ),
            expected_end_pose=str(
                shot.provenance.get("expected_end_pose") or shot.action.subject_action
            ),
            camera_movement=shot.camera.movement,
            motion_intensity=shot.camera.movement_intensity,
            style_lock=visual_style,
            subject_priority=[str(value) for value in incoming.present_character_ids],
            character_state=[
                item.model_dump_json() for item in incoming.character_appearance_states
            ],
            prop_state=[item.model_dump_json() for item in incoming.props],
            environment_motion=list(incoming.environment_conditions),
            timing_beats=list(shot.provenance.get("timing_beats", [])),
            continuity_invariants=[
                "preserve character identity, face, skin tone, hair, clothing, body "
                "proportions, props, palette, and environment geometry",
                "end characters: "
                f"{','.join(str(value) for value in outgoing.present_character_ids)}; "
                f"location: {outgoing.location_id}; emotion: {outgoing.emotional_state}",
            ],
            negative_motion_constraints=list(
                shot.provenance.get("negative_motion_constraints", [])
            ),
        )

    @staticmethod
    def _generation_parameters(
        request: VideoProviderRequest, shot: StoryboardShot, prompt_hash: str
    ) -> dict[str, object]:
        return {
            "provider": request.provider.value,
            "model": request.model.value,
            "remote_configuration_version": request.provider_configuration_version,
            "requested_duration": request.requested_duration_seconds,
            "dimensions": [request.width, request.height],
            "motion_prompt_hash": prompt_hash,
            "capability_profile_hash": shot.capability_hash,
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "validation_version": VALIDATION_VERSION,
            "application_idempotency_key": request.application_idempotency_key,
        }

    @staticmethod
    def _result(
        item: AnimationItem,
        video: AnimationGeneratedVideo,
        status: str,
        canonical_shot_id: UUID,
    ) -> ShotAnimationResult:
        from vidgen.contracts.animation import VideoValidationReport

        return ShotAnimationResult(
            shot_id=canonical_shot_id,
            status=status,  # type: ignore[arg-type]
            remote_task_id=video.remote_task_id,
            candidate=GeneratedVideoCandidate(
                generated_video_id=video.id,
                original_asset_id=video.original_asset_id,
                canonical_asset_id=video.canonical_asset_id,
                shot_id=canonical_shot_id,
                selected=video.selected,
                validation=VideoValidationReport.model_validate(video.validation_report),
            ),
        )
