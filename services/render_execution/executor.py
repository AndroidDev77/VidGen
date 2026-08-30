"""The canonical T17b render-execution service.

One function performs the whole operation, and every entry point calls it:

    authoritative project inputs
      -> canonical caption track
      -> RenderManifest
      -> DeterministicRenderPipeline
      -> verified final media assets
      -> completed render job
      -> available to T22 and T18

The design rules this module exists to enforce:

* **Resolve, then freeze.** Inputs are resolved and hashed *before* FFmpeg
  starts, and the manifest is immutable from that point on. Nothing queries for
  a "latest" anything once rendering has begun.
* **Checkpoint before you work.** Every stage transition is committed before the
  work it describes, so an interrupted run resumes from the last durable point
  instead of starting over.
* **Complete last.** The job is marked complete only after FFmpeg succeeded,
  verification passed, every canonical output is stored through
  :class:`~vidgen.storage.asset_service.AssetService`, and the final asset reads
  back through the normal storage interface.
* **Deterministic failures do not retry.** A lineage or validation refusal is
  terminal until an input changes; only genuinely transient failures leave the
  job reclaimable.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.render_execution.claims import (
    Claim,
    RenderClaimError,
    checkpoint,
    claim_render_job,
    heartbeat,
    require_lease,
)
from services.render_execution.ffmpeg import (
    CancellableCommandExecutor,
    RenderCancelled,
    RenderTimeout,
)
from services.render_execution.inputs import (
    ResolvedRenderInputs,
    render_settings_for,
    resolve_render_inputs,
)
from services.render_execution.manifest_builder import (
    BuiltManifest,
    CaptionArtifacts,
    build_captions,
    build_manifest,
)
from services.renderer.manifest import canonical_json
from services.renderer.pipeline import (
    AssetServiceArtifactStore,
    CompletedRender,
    DeterministicRenderPipeline,
    PersistedArtifact,
)
from services.renderer.selection import RenderLineageError
from services.renderer.verify import RenderVerificationError, run_bounded
from vidgen.contracts.render import (
    CaptionTrack,
    RenderFailure,
    RenderInputReference,
    RenderManifest,
)
from vidgen.contracts.render_execution import (
    RenderExecutionRequest,
    RenderExecutionResult,
    RenderExecutionStatus,
)
from vidgen.db.models import Asset, RenderJob
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.render_repository import RenderRepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.context import telemetry_context
from vidgen.telemetry.metrics import Metrics

logger = logging.getLogger(__name__)

#: The renderer this executor drives. It is recorded on the completed job so a
#: deliverable can always be traced back to the code that produced it.
RENDERER_VERSION = "t17/1"
EXECUTOR_VERSION = "t17b/1"

#: Progress checkpoints, as whole percentages. They are coarse on purpose: this
#: is a resume point and a status line, not a frame counter.
PROGRESS = {
    RenderExecutionStatus.CLAIMING: 1,
    RenderExecutionStatus.PREPARING: 10,
    RenderExecutionStatus.MANIFEST_READY: 25,
    RenderExecutionStatus.RENDERING: 40,
    RenderExecutionStatus.VERIFYING: 80,
    RenderExecutionStatus.PERSISTING: 90,
    RenderExecutionStatus.COMPLETE: 100,
}


class RenderExecutionError(RuntimeError):
    """A structured render-execution failure carrying its classification."""

    def __init__(self, failure: RenderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _failure(
    classification: str, code: str, message: str, *, retryable: bool = False
) -> RenderFailure:
    return RenderFailure(
        classification=classification,  # type: ignore[arg-type]
        code=code[:128],
        message=message[:1024] or code,
        retryable=retryable,
    )


class _RenderExecutionArtifactStore(AssetServiceArtifactStore):
    """Persist render outputs, reusing caption assets already stored this run.

    T17's pipeline serializes and stores the caption files itself. T17b has to
    store them earlier, because the immutable manifest references them by asset
    ID and content hash. Returning the already-persisted artifacts here keeps
    both facts true without a second asset row per retry.
    """

    def __init__(
        self,
        service: AssetService,
        job: RenderJob,
        cache_root: Path,
        caption_assets: dict[str, PersistedArtifact],
    ) -> None:
        super().__init__(service, job, cache_root)
        self._captions = caption_assets

    def store_bytes(
        self, *, content: bytes, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact:
        existing = self._captions.get(kind)
        if existing is not None:
            # Return the artifact resolved through the cache, so its ``path``
            # is a real readable file like every other artifact's.
            return self._artifact(existing.asset_id)
        return super().store_bytes(
            content=content, media_type=media_type, kind=kind, identity_key=identity_key
        )


@dataclass(frozen=True, slots=True)
class _PreparedRender:
    resolved: ResolvedRenderInputs
    built: BuiltManifest
    caption_assets: dict[str, PersistedArtifact]


class RenderExecutor:
    """Execute one queued render job, exactly once, against T17's pipeline."""

    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        *,
        work_root: Path,
        metrics: Metrics | None = None,
        preserve_failed_attempts: bool = False,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.assets = AssetService(session, blob_store)
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.metrics = metrics or Metrics()
        self.preserve_failed_attempts = preserve_failed_attempts

    # -- public API ---------------------------------------------------------

    def execute(self, request: RenderExecutionRequest) -> RenderExecutionResult:
        job = self.session.get(RenderJob, request.render_job_id)
        if job is None:
            raise RenderExecutionError(
                _failure("validation", "render_job_not_found", "the render job does not exist")
            )
        with telemetry_context(
            projectId=str(job.project_id),
            renderJobId=str(job.id),
            operation="render_execution",
            **{key: value for key, value in request.trace_context.items() if key != "traceparent"},
        ):
            return self._execute(job, request)

    # -- lifecycle ----------------------------------------------------------

    def _execute(self, job: RenderJob, request: RenderExecutionRequest) -> RenderExecutionResult:
        if job.status == RenderExecutionStatus.COMPLETE.value:
            logger.info("render job already complete", extra={"status": "reused"})
            return self._completed_result(job, reused=True)
        if job.cancel_requested or job.status == RenderExecutionStatus.CANCELLED.value:
            # Cancellation is durable state, not just a return value: a job
            # asked to stop must read as cancelled to T18 and to the next worker.
            if job.status != RenderExecutionStatus.CANCELLED.value:
                job.status = RenderExecutionStatus.CANCELLED.value
                job.checkpoint = "cancelled"
                job.error_code = "render_cancelled"
                job.failure_classification = "cancelled"
                job.claimed_by = None
                job.lease_expires_at = None
                job.completed_at = job.completed_at or datetime.now(UTC)
                self.session.flush()
                self.session.commit()
            return self._cancelled_result(job)
        try:
            claim = claim_render_job(
                self.session,
                render_job_id=job.id,
                worker_id=request.worker_id,
                lease_seconds=request.lease_seconds,
                max_attempts=request.max_attempts,
            )
        except RenderClaimError as error:
            if error.code == "render_job_complete":
                self.session.rollback()
                return self._completed_result(self._reload(job.id), reused=True)
            if error.code == "render_job_cancelled":
                self.session.rollback()
                return self._cancelled_result(self._reload(job.id))
            self.session.rollback()
            raise RenderExecutionError(
                _failure(
                    "transient" if error.code == "render_job_leased" else "validation",
                    error.code,
                    str(error),
                    retryable=error.code == "render_job_leased",
                )
            ) from error
        self.session.commit()
        logger.info("render job claimed", extra={"status": "claimed"})
        attempt_root: Path | None = None
        try:
            prepared = self._prepare(job, claim, request)
            attempt_root = self.work_root / f"job-{job.id}"
            completed = self._render(job, claim, request, prepared, attempt_root)
            result = self._complete(job, claim, prepared, completed)
            self.session.commit()
            logger.info("render job complete", extra={"status": "complete"})
            return result
        except RenderCancelled:
            self.session.rollback()
            return self._record_cancellation(job.id, claim)
        except (RenderLineageError, RenderVerificationError, RenderExecutionError) as error:
            self.session.rollback()
            failure = (
                error.failure
                if isinstance(error, RenderExecutionError)
                else _failure(
                    "lineage" if isinstance(error, RenderLineageError) else "validation",
                    getattr(error, "code", "render_verification_failed"),
                    str(error),
                )
            )
            return self._record_failure(job.id, claim, failure)
        except RenderTimeout as error:
            self.session.rollback()
            return self._record_failure(
                job.id,
                claim,
                _failure("transient", "render_timeout", str(error), retryable=True),
            )
        except RenderClaimError as error:
            self.session.rollback()
            return self._record_failure(
                job.id, claim, _failure("transient", error.code, str(error), retryable=True)
            )
        except Exception as error:  # classified structurally, never re-raised raw
            self.session.rollback()
            return self._record_failure(
                job.id,
                claim,
                _failure("execution", "render_execution_failed", str(error), retryable=True),
            )
        finally:
            if attempt_root is not None and not self.preserve_failed_attempts:
                shutil.rmtree(attempt_root, ignore_errors=True)

    # -- stages -------------------------------------------------------------

    def _prepare(
        self, job: RenderJob, claim: Claim, request: RenderExecutionRequest
    ) -> _PreparedRender:
        checkpoint(
            self.session,
            claim=claim,
            status=RenderExecutionStatus.PREPARING,
            phase="resolve_inputs",
            progress_percent=PROGRESS[RenderExecutionStatus.PREPARING],
        )
        self.session.commit()
        settings = render_settings_for(job)
        resolved = resolve_render_inputs(self.session, job=job, settings=settings)
        job = require_lease(self.session, claim)
        if job.input_hash and job.input_hash != resolved.input_hash:
            raise RenderExecutionError(
                _failure(
                    "lineage",
                    "input_identity_changed",
                    "the render job's stored input identity no longer matches the "
                    "project's authoritative inputs; queue a new render job",
                )
            )
        job.input_hash = resolved.input_hash
        job.input_selection = resolved.contract.model_dump(mode="json")
        job.script_id = resolved.selection.script.id
        job.script_version = resolved.selection.script.version
        job.narration_run_id = resolved.selection.narration.id
        job.storyboard_run_id = resolved.selection.storyboard.id
        job.t16_result_reference = f"t16:{resolved.selection.storyboard.id}"
        job.expected_duration_us = resolved.total_duration_us
        job.renderer_version = RENDERER_VERSION
        job.trace_id = request.trace_context.get("trace_id") or job.trace_id or None
        self.session.flush()
        self.session.commit()
        logger.info("render inputs resolved", extra={"status": "prepared"})

        captions = build_captions(resolved)
        caption_assets = self._persist_captions(job, resolved, captions)
        manifest = build_manifest(
            resolved,
            captions,
            {
                role: RenderInputReference(
                    asset_id=artifact.asset_id,
                    sha256=artifact.sha256,
                    media_type=artifact.media_type,
                    role=role,
                )
                for role, artifact in caption_assets.items()
            },
        )
        self._bind_manifest(job, claim, manifest, captions, caption_assets)
        checkpoint(
            self.session,
            claim=claim,
            status=RenderExecutionStatus.MANIFEST_READY,
            phase="manifest_ready",
            progress_percent=PROGRESS[RenderExecutionStatus.MANIFEST_READY],
        )
        self.session.commit()
        logger.info("render manifest built", extra={"status": "manifest_ready"})
        return _PreparedRender(
            resolved=resolved,
            built=BuiltManifest(manifest=manifest, captions=captions),
            caption_assets=caption_assets,
        )

    def _persist_captions(
        self, job: RenderJob, resolved: ResolvedRenderInputs, captions: CaptionArtifacts
    ) -> dict[str, PersistedArtifact]:
        """Store the deliverable caption assets, reusing them across retries.

        The idempotency key is derived from the input identity and the caption
        configuration, so an interrupted run that already produced the caption
        track finds the same asset instead of writing a second one.
        """
        payloads = captions.payloads()
        media_types = {
            "caption_srt": "application/x-subrip",
            "caption_webvtt": "text/vtt",
            "caption_ass": "text/x-ssa",
        }
        roles = ["caption_srt", "caption_webvtt"]
        if resolved.settings.subtitle_mode in {"burn_in", "both"}:
            roles.append("caption_ass")
        stored: dict[str, PersistedArtifact] = {}
        for role in roles:
            asset = self.assets.store(
                content=payloads[role],
                kind=role,
                media_type=media_types[role],
                project_id=job.project_id,
                idempotency_key=f"t17b:{resolved.input_hash}:{role}",
                metadata={
                    "render_job_id": str(job.id),
                    "caption_track_id": str(captions.track.caption_track_id),
                    "input_hash": resolved.input_hash,
                    "executor_version": EXECUTOR_VERSION,
                },
            )
            stored[role] = PersistedArtifact(
                asset_id=asset.id,
                sha256=asset.sha256,
                media_type=asset.media_type,
                # The caption bytes are already in storage; the pipeline
                # resolves a readable path through the artifact store when it
                # needs one, so nothing here depends on this being on disk.
                path=self.work_root / "captions" / asset.sha256,
            )
        report = self.assets.store(
            content=canonical_json(captions.validation),
            kind="caption_validation_report",
            media_type="application/vnd.vidgen.caption-validation+json",
            project_id=job.project_id,
            idempotency_key=f"t17b:{resolved.input_hash}:caption-validation",
            metadata={"render_job_id": str(job.id), "input_hash": resolved.input_hash},
        )
        job.srt_asset_id = stored["caption_srt"].asset_id
        job.webvtt_asset_id = stored["caption_webvtt"].asset_id
        if "caption_ass" in stored:
            job.ass_asset_id = stored["caption_ass"].asset_id
        job.caption_profile = {
            "subtitle_mode": resolved.settings.subtitle_mode,
            "language": resolved.settings.language,
            "mode": resolved.settings.subtitle_mode,
            "configuration_hash": resolved.contract.caption_configuration_hash,
        }
        self._upsert_caption_track(job, resolved, captions, stored, report.id)
        self.session.flush()
        logger.info("canonical caption track persisted", extra={"status": "captions_ready"})
        return stored

    def _upsert_caption_track(
        self,
        job: RenderJob,
        resolved: ResolvedRenderInputs,
        captions: CaptionArtifacts,
        stored: dict[str, PersistedArtifact],
        validation_asset_id: UUID,
    ) -> None:
        record = self.session.scalar(
            select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == job.id)
        )
        values = {
            "narration_run_id": resolved.selection.narration.id,
            "caption_identity": captions.validation.caption_identity,
            "language": captions.track.language,
            "cue_count": len(captions.track.cues),
            "start_us": captions.track.cues[0].start_us,
            "end_us": captions.track.cues[-1].end_us,
            "srt_asset_id": stored["caption_srt"].asset_id,
            "webvtt_asset_id": stored["caption_webvtt"].asset_id,
            "ass_asset_id": stored["caption_ass"].asset_id if "caption_ass" in stored else None,
            "validation_report_asset_id": validation_asset_id,
            "configuration_hash": resolved.contract.caption_configuration_hash,
        }
        if record is None:
            self.session.add(CaptionTrackRecord(render_job_id=job.id, **values))
        else:
            for key, value in values.items():
                setattr(record, key, value)
        self.session.flush()

    def _bind_manifest(
        self,
        job: RenderJob,
        claim: Claim,
        manifest: RenderManifest,
        captions: CaptionArtifacts,
        caption_assets: dict[str, PersistedArtifact],
    ) -> None:
        del captions, caption_assets
        owner = self.session.scalar(
            select(RenderJob).where(
                RenderJob.render_identity == manifest.render_identity, RenderJob.id != job.id
            )
        )
        if owner is not None:
            raise RenderExecutionError(
                _failure(
                    "lineage",
                    "duplicate_render_identity",
                    f"render job {owner.id} already owns this render identity; "
                    "execute or download that job instead of rendering it twice",
                )
            )
        job = require_lease(self.session, claim)
        job.render_identity = manifest.render_identity
        job.idempotency_key = job.idempotency_key or manifest.idempotency_key
        job.video_profile = {
            "name": manifest.provenance.get("render_profile", "1080p24"),
            "width": manifest.video_profile.width,
            "height": manifest.video_profile.height,
            "frame_rate": manifest.video_profile.frame_rate,
            "codec": manifest.video_profile.codec,
            "pixel_format": manifest.video_profile.pixel_format,
        }
        job.audio_profile = {
            "codec": manifest.audio_profile.codec,
            "sample_rate_hz": manifest.audio_profile.sample_rate_hz,
            "channels": manifest.audio_profile.channels,
            "integrated_loudness_lufs": float(manifest.audio_profile.integrated_lufs),
            "true_peak_dbtp": float(manifest.audio_profile.true_peak_dbtp),
        }
        self.session.flush()

    def _render(
        self,
        job: RenderJob,
        claim: Claim,
        request: RenderExecutionRequest,
        prepared: _PreparedRender,
        attempt_root: Path,
    ) -> CompletedRender:
        manifest = prepared.built.manifest
        checkpoint(
            self.session,
            claim=claim,
            status=RenderExecutionStatus.RENDERING,
            phase="ffmpeg",
            progress_percent=PROGRESS[RenderExecutionStatus.RENDERING],
        )
        self.session.commit()
        attempt_root.mkdir(parents=True, exist_ok=True)
        job = require_lease(self.session, claim)
        recovered = self._recover_persisted_outputs(job, manifest)
        if recovered is not None:
            # The previous attempt got as far as committing every output. There
            # is nothing left to render: re-encoding identical inputs would burn
            # CPU to produce the bytes already in storage.
            logger.info("render outputs recovered", extra={"status": "recovered"})
            return recovered
        self._require_disk_space(attempt_root, request.minimum_free_bytes)
        repository = RenderRepository(self.session)
        attempt_row = repository.next_attempt(job, manifest.render_identity)
        self.session.commit()

        def on_heartbeat(phase: str) -> None:
            heartbeat(
                self.session,
                claim=claim,
                lease_seconds=request.lease_seconds,
                phase=phase,
                progress_percent=PROGRESS[RenderExecutionStatus.RENDERING],
            )
            self.session.commit()

        def is_cancelled() -> bool:
            self.session.expire_all()
            current = self.session.get(RenderJob, claim.render_job_id)
            self.session.commit()
            return bool(current is not None and current.cancel_requested)

        executor = CancellableCommandExecutor(
            timeout_seconds=request.execution_timeout_seconds,
            heartbeat=on_heartbeat,
            cancelled=is_cancelled,
        )
        store = _RenderExecutionArtifactStore(
            self.assets, job, attempt_root / "cache", prepared.caption_assets
        )
        pipeline = DeterministicRenderPipeline(
            store=store,
            work_root=attempt_root / "work",
            executor=executor,
            preserve_failed_attempts=self.preserve_failed_attempts,
        )
        started = time.monotonic()
        logger.info("ffmpeg render started", extra={"status": "rendering"})
        completed = pipeline.run(
            manifest=manifest,
            caption_track=prepared.built.captions.track,
            resolve_asset=self._asset_resolver(job.project_id),
        )
        elapsed = time.monotonic() - started
        if manifest.narration_duration_us:
            self.metrics.render_factor.observe(
                elapsed / (manifest.narration_duration_us / 1_000_000)
            )
        attempt_row.status = RenderExecutionStatus.COMPLETE.value
        attempt_row.completed_at = datetime.now(UTC)
        attempt_row.ffmpeg_version = _tool_version("ffmpeg")
        attempt_row.operational_metadata = {
            "executor_version": EXECUTOR_VERSION,
            "phase_seconds": {record.phase: record.duration_seconds for record in executor.phases},
            "ffmpeg_executions": executor.executions,
            "wall_clock_seconds": round(elapsed, 3),
            "reused": completed.reused,
        }
        self.session.flush()
        self.metrics.render_verification.labels(status="passed").inc()
        logger.info("render verified", extra={"status": "verified"})
        return completed

    def _recover_persisted_outputs(
        self, job: RenderJob, manifest: RenderManifest
    ) -> CompletedRender | None:
        """Resume a run that rendered and persisted, then failed before completing.

        Recovery is only safe when the stored outputs belong to *this* manifest:
        the job's render identity must match, every canonical asset row must
        exist, and its bytes must still be readable. Anything less and the render
        is redone rather than a stale output being blessed as the deliverable.
        """
        if job.render_identity != manifest.render_identity:
            return None
        references = (
            job.manifest_asset_id,
            job.srt_asset_id,
            job.webvtt_asset_id,
            job.final_video_asset_id,
            job.verification_report_asset_id,
        )
        if any(asset_id is None for asset_id in references) or not job.measured_duration_us:
            return None
        artifacts: list[PersistedArtifact] = []
        for asset_id in references:
            asset = self.session.get(Asset, asset_id)
            if asset is None or not self.blob_store.exists(asset.storage_key):
                return None
            artifacts.append(
                PersistedArtifact(
                    asset_id=asset.id,
                    sha256=asset.sha256,
                    media_type=asset.media_type,
                    path=self.work_root / "recovered" / asset.sha256,
                )
            )
        manifest_asset, srt, webvtt, final_video, report = artifacts
        return CompletedRender(
            render_identity=manifest.render_identity,
            manifest=manifest_asset,
            srt=srt,
            webvtt=webvtt,
            final_video=final_video,
            verification_report=report,
            measured_duration_us=job.measured_duration_us,
            reused=True,
        )

    def _complete(
        self,
        job: RenderJob,
        claim: Claim,
        prepared: _PreparedRender,
        completed: CompletedRender,
    ) -> RenderExecutionResult:
        checkpoint(
            self.session,
            claim=claim,
            status=RenderExecutionStatus.PERSISTING,
            phase="persist_outputs",
            progress_percent=PROGRESS[RenderExecutionStatus.PERSISTING],
        )
        self.session.flush()
        job = require_lease(self.session, claim)
        job.manifest_asset_id = completed.manifest.asset_id
        job.srt_asset_id = completed.srt.asset_id
        job.webvtt_asset_id = completed.webvtt.asset_id
        job.final_video_asset_id = completed.final_video.asset_id
        job.output_asset_id = completed.final_video.asset_id
        job.verification_report_asset_id = completed.verification_report.asset_id
        job.premaster_audio_asset_id = (
            completed.premaster_audio.asset_id if completed.premaster_audio else None
        )
        job.output_sha256 = completed.final_video.sha256
        job.measured_duration_us = completed.measured_duration_us
        job.expected_duration_us = prepared.built.manifest.narration_duration_us
        job.ffmpeg_version = _tool_version("ffmpeg")
        job.renderer_version = RENDERER_VERSION
        job.pipeline_version = prepared.built.manifest.pipeline_version
        job.progress_percent = PROGRESS[RenderExecutionStatus.COMPLETE]
        job.checkpoint = "complete"
        job.status = RenderExecutionStatus.COMPLETE.value
        job.completed_at = job.completed_at or datetime.now(UTC)
        job.error_code = None
        job.failure_classification = None
        job.claimed_by = None
        job.lease_expires_at = None
        self._select_render(job)
        self._require_readable(job)
        self.session.flush()
        return self._completed_result(job, reused=completed.reused)

    def _select_render(self, job: RenderJob) -> None:
        """Make this render the project's current one.

        The partial unique index allows exactly one selected render per project,
        so the previous selection is cleared in the same transaction. T22 and
        T18 both read the selected render, which is what binds a final-QA
        decision and an approval to this exact render rather than an older one.
        """
        for other in self.session.scalars(
            select(RenderJob).where(
                RenderJob.project_id == job.project_id,
                RenderJob.selected.is_(True),
                RenderJob.id != job.id,
            )
        ):
            other.selected = False
        self.session.flush()
        job.selected = True
        self.session.flush()

    def _require_readable(self, job: RenderJob) -> None:
        """Prove the deliverable reads back through the normal asset interface."""
        asset = self.session.get(Asset, job.final_video_asset_id)
        if asset is None or not self.blob_store.exists(asset.storage_key):
            raise RenderExecutionError(
                _failure(
                    "execution",
                    "final_asset_unreadable",
                    "the final render is not readable through asset storage",
                    retryable=True,
                )
            )

    # -- terminal states ----------------------------------------------------

    def _record_failure(
        self, render_job_id: UUID, claim: Claim, failure: RenderFailure
    ) -> RenderExecutionResult:
        job = self._reload(render_job_id)
        job.status = RenderExecutionStatus.FAILED.value
        job.error_code = failure.code
        job.failure_classification = failure.classification
        job.checkpoint = "failed"
        job.error = {
            "code": failure.code,
            "classification": failure.classification,
            "message": failure.message[:1024],
            "retryable": failure.retryable,
            "warnings": list((job.error or {}).get("warnings", []))[:32],
        }
        job.completed_at = datetime.now(UTC)
        if job.claimed_by == claim.worker_id:
            job.claimed_by = None
            job.lease_expires_at = None
        self.session.flush()
        self.session.commit()
        self.metrics.render_verification.labels(status="failed").inc()
        logger.warning(
            "render job failed",
            extra={"status": "failed", "errorCode": failure.code},
        )
        return RenderExecutionResult(
            render_job_id=job.id,
            project_id=job.project_id,
            status=RenderExecutionStatus.FAILED,
            input_hash=job.input_hash,
            render_identity=job.render_identity,
            attempt=job.attempt_count,
            failure=failure,
        )

    def _record_cancellation(self, render_job_id: UUID, claim: Claim) -> RenderExecutionResult:
        job = self._reload(render_job_id)
        job.status = RenderExecutionStatus.CANCELLED.value
        job.checkpoint = "cancelled"
        job.error_code = "render_cancelled"
        job.failure_classification = "cancelled"
        job.completed_at = datetime.now(UTC)
        if job.claimed_by == claim.worker_id:
            job.claimed_by = None
            job.lease_expires_at = None
        self.session.flush()
        self.session.commit()
        logger.info("render job cancelled", extra={"status": "cancelled"})
        return self._cancelled_result(job)

    def _cancelled_result(self, job: RenderJob) -> RenderExecutionResult:
        return RenderExecutionResult(
            render_job_id=job.id,
            project_id=job.project_id,
            status=RenderExecutionStatus.CANCELLED,
            input_hash=job.input_hash,
            render_identity=job.render_identity,
            attempt=job.attempt_count,
            failure=_failure("cancelled", "render_cancelled", "the render job was cancelled"),
        )

    def _completed_result(self, job: RenderJob, *, reused: bool) -> RenderExecutionResult:
        return RenderExecutionResult(
            render_job_id=job.id,
            project_id=job.project_id,
            status=RenderExecutionStatus.COMPLETE,
            reused=reused,
            render_identity=job.render_identity,
            input_hash=job.input_hash,
            output_sha256=job.output_sha256,
            manifest_asset_id=job.manifest_asset_id,
            caption_srt_asset_id=job.srt_asset_id,
            caption_webvtt_asset_id=job.webvtt_asset_id,
            final_video_asset_id=job.final_video_asset_id,
            verification_report_asset_id=job.verification_report_asset_id,
            measured_duration_us=job.measured_duration_us,
            expected_duration_us=job.expected_duration_us,
            renderer_version=job.renderer_version or RENDERER_VERSION,
            ffmpeg_version=job.ffmpeg_version,
            attempt=job.attempt_count,
            completed_at=job.completed_at,
        )

    # -- helpers ------------------------------------------------------------

    def _reload(self, render_job_id: UUID) -> RenderJob:
        job = self.session.get(RenderJob, render_job_id)
        if job is None:  # pragma: no cover - the row cannot vanish mid-transaction
            raise RenderExecutionError(
                _failure("validation", "render_job_not_found", "the render job does not exist")
            )
        return job

    def _asset_resolver(self, project_id: UUID):  # type: ignore[no-untyped-def]
        """Stream one owned asset into the attempt directory, never into memory."""

        def resolve(asset_id: UUID, destination: Path) -> None:
            asset = self.session.get(Asset, asset_id)
            if asset is None:
                raise RenderExecutionError(
                    _failure("validation", "input_asset_missing", f"asset {asset_id} is missing")
                )
            if asset.project_id is not None and asset.project_id != project_id:
                raise RenderExecutionError(
                    _failure(
                        "lineage",
                        "cross_project_asset",
                        f"asset {asset_id} belongs to another project",
                    )
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.blob_store.copy_to(asset.storage_key, destination)

        return resolve

    @staticmethod
    def _require_disk_space(root: Path, minimum_free_bytes: int) -> None:
        if minimum_free_bytes <= 0:
            return
        usage = shutil.disk_usage(root)
        if usage.free < minimum_free_bytes:
            raise RenderExecutionError(
                _failure(
                    "execution",
                    "insufficient_temporary_storage",
                    f"render needs {minimum_free_bytes} free bytes under {root}; "
                    f"{usage.free} are available",
                    retryable=True,
                )
            )


def _tool_version(tool: str) -> str:
    """The first banner line of a local tool, bounded and never a full log."""
    try:
        # ``run_bounded`` keeps the *tail* of the output, which is the right
        # choice for an FFmpeg error and the wrong one for a banner, so the
        # limit is generous and the first line is taken from the head.
        result = run_bounded([tool, "-version"], timeout=30, output_limit=100_000)
    except (OSError, ValueError):  # pragma: no cover - only a broken installation
        return "unknown"
    line = (result.stdout or "").splitlines()
    return line[0][:255] if line else "unknown"


def caption_track_of(prepared: _PreparedRender) -> CaptionTrack:  # pragma: no cover - readability
    return prepared.built.captions.track


def render_progress_payload(job: RenderJob) -> str:  # pragma: no cover - operator convenience
    return json.dumps(
        {
            "render_job_id": str(job.id),
            "status": job.status,
            "progress_percent": job.progress_percent,
            "checkpoint": job.checkpoint,
        },
        sort_keys=True,
    )
