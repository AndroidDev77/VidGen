"""Restartable segment-at-a-time T12 orchestration."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.narration.alignment import FakeAligner, NarrationAligner
from services.narration.normalization import normalize_audio, probe_audio
from services.narration.preview import concatenate_preview
from services.narration.providers import NarrationProvider
from services.narration.quality import QualityThresholds, validate_quality
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.narration import (
    NarrationAlignment,
    NarrationPreviewManifest,
    NarrationProviderRequest,
    NarrationProviderResult,
    NarrationQualityReport,
    NarrationResult,
    NarrationSegmentResult,
)
from vidgen.db.cost_models import ProjectBudget, ProviderPriceRate
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.db.models import Asset, Project
from vidgen.db.narration_models import (
    NarrationAttemptRecord,
    NarrationRun,
    NarrationSegment,
    VoiceProfileRecord,
)
from vidgen.db.narration_repository import NarrationRepository
from vidgen.db.script_models import Script, ScriptSegment
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

PIPELINE_VERSION = "narration/1.0.0"
DEFAULT_QUALITY_THRESHOLDS = QualityThresholds()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class NarrationPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: NarrationProvider,
        *,
        thresholds: QualityThresholds = DEFAULT_QUALITY_THRESHOLDS,
        metrics: Metrics | None = None,
        aligner: NarrationAligner | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.thresholds = thresholds
        self.metrics = metrics or Metrics()
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.narration")
        if aligner is None and provider.name != "fake":
            raise ValueError("production narration requires a configured timestamp aligner")
        self.aligner = aligner or FakeAligner()
        self.cancellation_check = cancellation_check or (lambda: False)
        self.repo = NarrationRepository(session)
        self.assets = AssetService(session, blob_store)

    async def process(
        self, *, project_id: UUID, voice_profile_id: UUID, idempotency_key: str
    ) -> NarrationResult:
        try:
            return await self._process(
                project_id=project_id,
                voice_profile_id=voice_profile_id,
                idempotency_key=idempotency_key,
            )
        except BaseException as error:
            run = self.repo.run_by_key(project_id, idempotency_key)
            project = self.session.get(Project, project_id)
            if run is not None:
                run.status = "narration_failed"
                run.error_code = type(error).__name__[:128]
            if project is not None:
                project.status = "narration_failed"
            self.session.commit()
            raise

    async def _process(
        self, *, project_id: UUID, voice_profile_id: UUID, idempotency_key: str
    ) -> NarrationResult:
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError("project does not exist")
        script, source_segments = self.repo.authoritative_script(project_id)
        profile = self.repo.voice_profile(voice_profile_id, project_id)
        cfg = profile.configuration
        material = {
            "project": project_id,
            "script": script.id,
            "version": script.version,
            "voice": profile.id,
            "voice_version": profile.version,
            "voice_hash": profile.configuration_hash,
            "provider": self.provider.name,
            "model": profile.model,
            "quality": self.thresholds.__dict__,
            "pipeline": PIPELINE_VERSION,
        }
        input_hash = canonical_hash(material)
        run = self.repo.run_by_key(project_id, idempotency_key)
        if run and run.input_hash != input_hash:
            raise ValueError("idempotency key was used with stale narration inputs")
        if run is None:
            run = NarrationRun(
                project_id=project_id,
                script_id=script.id,
                script_version=script.version,
                voice_profile_id=profile.id,
                voice_profile_version=profile.version,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                status="narration_queued",
                pipeline_version=PIPELINE_VERSION,
                parameters=material,
            )
            self.session.add(run)
            self.session.flush()
        if run.status == "narration_complete":
            return self._result(run, project_id)
        project.status = run.status = "narration_generating"
        self.session.commit()
        with tempfile.TemporaryDirectory(prefix="vidgen-narration-") as temporary:
            root = Path(temporary)
            selected = []
            for source in source_segments:
                if self.cancellation_check():
                    raise RuntimeError("narration activity cancelled")
                text_hash = hashlib.sha256(" ".join(source.text.split()).encode()).hexdigest()
                identity = canonical_hash(
                    {
                        **material,
                        "segment": source.id,
                        "sequence": source.sequence,
                        "text": text_hash,
                        "instructions": source.voice_direction,
                        "speed": cfg.get("default_pace", 1),
                        "format": cfg.get("output_format", "wav"),
                    }
                )
                row = self.repo.segment_by_identity(run.id, identity)
                if row is None:
                    reusable = self.repo.reusable_segment(identity)
                    row = NarrationSegment(
                        narration_run_id=run.id,
                        script_segment_id=source.id,
                        sequence=source.sequence,
                        text_hash=text_hash,
                        generation_identity=identity,
                        status="complete" if reusable is not None else "pending",
                        selected_attempt_id=None,
                        reused_from_segment_id=reusable.id if reusable else None,
                        original_asset_id=reusable.original_asset_id if reusable else None,
                        normalized_asset_id=reusable.normalized_asset_id if reusable else None,
                        duration_seconds=reusable.duration_seconds if reusable else None,
                        alignment=reusable.alignment if reusable else None,
                        quality_report=reusable.quality_report if reusable else None,
                        word_timings=reusable.word_timings if reusable else None,
                    )
                    self.session.add(row)
                    self.session.flush()
                if row.status != "complete":
                    await self._generate(row, source, script, profile, cfg, root, project)
                selected.append(row)
            project.status = run.status = "narration_previewing"
            self.session.commit()
            preview_paths = []
            for row in selected:
                asset = self.session.get(Asset, row.normalized_asset_id)
                assert asset is not None
                path = root / f"segment-{row.sequence:06}.wav"
                self.blob_store.copy_to(asset.storage_key, path)
                preview_paths.append(path)
            preview_path = root / "preview.wav"
            preview_probe = concatenate_preview(preview_paths, preview_path, root)
            parent_ids = tuple(
                row.normalized_asset_id for row in selected if row.normalized_asset_id
            )
            preview = self.assets.store_file(
                path=preview_path,
                kind="audio",
                media_type="audio/wav",
                project_id=project_id,
                parent_asset_ids=parent_ids,
                idempotency_key=f"{run.id}:preview",
                generation_parameters={"concat": "copy", "order": "sequence"},
            )
            manifest = NarrationPreviewManifest(
                script_id=script.id,
                script_version=script.version,
                narration_run_id=run.id,
                voice_profile_id=profile.id,
                voice_profile_version=profile.version,
                segment_ids=[row.script_segment_id for row in selected],
                narration_asset_ids=[asset_id for asset_id in parent_ids],
                segment_durations_seconds=[
                    row.duration_seconds for row in selected if row.duration_seconds is not None
                ],
                word_timing_references=[row.id for row in selected],
                concatenation_parameters={"codec": "copy", "order": "canonical_sequence"},
                preview_duration_seconds=preview_probe.duration_seconds,
                preview_asset_id=preview.id,
                input_hash=canonical_hash([str(x) for x in parent_ids]),
                output_hash=preview.sha256,
                provenance={"pipeline_version": PIPELINE_VERSION},
            )
            manifest_asset = self.assets.store(
                content=manifest.model_dump_json().encode(),
                kind="json",
                media_type="application/json",
                project_id=project_id,
                parent_asset_ids=(preview.id, *parent_ids),
                idempotency_key=f"{run.id}:preview-manifest",
            )
            run.preview_asset_id = preview.id
            run.total_duration_seconds = preview_probe.duration_seconds
            run.parameters = {**material, "manifest_asset_id": str(manifest_asset.id)}
            self.session.query(NarrationRun).filter(
                NarrationRun.project_id == project_id,
                NarrationRun.script_id == script.id,
                NarrationRun.script_version == script.version,
                NarrationRun.id != run.id,
                NarrationRun.selected,
            ).update({"selected": False}, synchronize_session=False)
            run.status = project.status = "narration_complete"
            run.selected = True
            self.session.commit()
        return self._result(run, project_id)

    async def _generate(
        self,
        row: NarrationSegment,
        source: ScriptSegment,
        script: Script,
        profile: VoiceProfileRecord,
        cfg: dict[str, Any],
        root: Path,
        project: Project,
    ) -> None:
        text = str(source.text)
        if self.cancellation_check():
            raise RuntimeError("narration activity cancelled")
        previous_attempts = self.repo.attempts(row.id)
        interrupted = next((item for item in previous_attempts if item.completed_at is None), None)
        attempt_no = interrupted.attempt_number if interrupted else len(previous_attempts) + 1
        if attempt_no > 3:
            raise RuntimeError("narration segment exhausted three attempts")
        key = f"{row.generation_identity}:{attempt_no}"
        attempt = interrupted
        if attempt is None:
            retry_instructions = self._retry_instructions(previous_attempts)
            attempt = NarrationAttemptRecord(
                narration_segment_id=row.id,
                attempt_number=attempt_no,
                provider=self.provider.name,
                model=str(profile.model),
                provider_idempotency_key=key,
                voice_settings=cfg,
                instructions=" ".join(
                    filter(
                        None,
                        (
                            str(cfg.get("default_speaking_instructions", "")),
                            str(source.voice_direction),
                            retry_instructions,
                        ),
                    )
                ),
            )
            self.session.add(attempt)
            self.session.commit()  # durable pre-provider checkpoint
        original = root / f"{row.id}-{attempt_no}.provider"
        normalized = root / f"{row.id}-{attempt_no}.wav"
        request = NarrationProviderRequest(
            idempotency_key=key,
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            script_segment_id=source.id,
            segment_sequence=source.sequence,
            text=text,
            voice_profile_id=profile.id,
            voice_profile_version=profile.version,
            voice_id=str(profile.provider_voice_id),
            model=str(profile.model),
            speaking_instructions=attempt.instructions,
            speed=float(cfg.get("default_pace", 1)),
            output_format=str(cfg.get("output_format", "wav")),
            language=str(profile.language),
            attempt_number=attempt_no,
        )
        recovered_asset = (
            self.session.get(Asset, attempt.provider_output_asset_id)
            if attempt.provider_output_asset_id
            else None
        )
        rate = self.session.scalar(
            select(ProviderPriceRate)
            .where(
                ProviderPriceRate.provider == self.provider.name,
                ProviderPriceRate.model == str(profile.model),
                ProviderPriceRate.operation == "narration.generate",
                ProviderPriceRate.usage_unit == "AUDIO_OUTPUT_SECOND",
                ProviderPriceRate.active,
            )
            .order_by(ProviderPriceRate.effective_start.desc())
        )
        estimated_cost = (
            Decimal(str(source.estimated_duration_ms / 1000)) / rate.unit_size * rate.unit_price
            if rate is not None
            else Decimal("0")
        )
        if recovered_asset is not None:
            self.blob_store.copy_to(recovered_asset.storage_key, original)
            result = NarrationProviderResult(
                provider=attempt.provider,
                model=attempt.model,
                provider_request_id=attempt.provider_request_id or key,
                attempt_number=attempt_no,
                content_type=recovered_asset.media_type,
                audio_format=str(cfg.get("output_format", "wav")),
                byte_size=recovered_asset.byte_size,
                usage=attempt.usage,
                provider_duration_seconds=0,
                idempotency_key=key,
                warnings=["recovered durable provider output"],
            )
        else:
            result = await self._call_narration_provider(
                request=request,
                row=row,
                project=project,
                profile=profile,
                key=key,
                attempt_no=attempt_no,
                estimated_cost=estimated_cost,
                rate=rate,
                destination=original,
            )
        attempt.provider_request_id = result.provider_request_id
        attempt.usage = result.usage
        original_asset = self.assets.store_file(
            path=original,
            kind="audio",
            media_type=result.content_type,
            project_id=project.id,
            provider=result.provider,
            provider_request_id=result.provider_request_id,
            idempotency_key=f"{key}:original",
        )
        attempt.provider_output_asset_id = original_asset.id
        self.session.commit()  # provider response recovery checkpoint
        normalize_audio(original, normalized)
        probe = probe_audio(normalized)
        alignment = await self._align_instrumented(
            row=row,
            project=project,
            profile=profile,
            text=text,
            duration=probe.duration_seconds,
            path=normalized,
        )
        project.status = "narration_validating"
        quality = validate_quality(
            normalized, text, probe.duration_seconds, alignment, self.thresholds
        )
        normalized_asset = self.assets.store_file(
            path=normalized,
            kind="audio",
            media_type="audio/wav",
            project_id=project.id,
            parent_asset_ids=(original_asset.id,),
            provider=result.provider,
            provider_request_id=result.provider_request_id,
            idempotency_key=f"{key}:normalized",
            generation_parameters={"sample_rate": 48000, "channels": 1, "codec": "pcm_s16le"},
            metadata={"ffprobe": probe.raw},
        )
        attempt.normalized_asset_id = normalized_asset.id
        attempt.quality_result = quality.model_dump(mode="json")
        attempt.completed_at = datetime.now(UTC)
        if not quality.valid:
            attempt.failure_classification = "QUALITY"
            self.session.commit()
            return await self._generate(row, source, script, profile, cfg, root, project)
        row.status = "complete"
        row.selected_attempt_id = attempt.id
        row.original_asset_id = original_asset.id
        row.normalized_asset_id = normalized_asset.id
        row.duration_seconds = probe.duration_seconds
        row.alignment = alignment.model_dump(mode="json")
        row.quality_report = quality.model_dump(mode="json")
        row.word_timings = [x.model_dump(mode="json") for x in alignment.timings]
        self.session.commit()

    async def _call_narration_provider(
        self,
        *,
        request: NarrationProviderRequest,
        row: NarrationSegment,
        project: Project,
        profile: VoiceProfileRecord,
        key: str,
        attempt_no: int,
        estimated_cost: Decimal,
        rate: ProviderPriceRate | None,
        destination: Path,
    ) -> NarrationProviderResult:
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=project.id,
            provider=self.provider.name,
            model=str(profile.model),
            operation="narration.generate",
            input_hash=row.generation_identity,
            idempotency_key=key,
            related_entity_id=row.id,
            attempt_number=attempt_no,
            estimated_cost=estimated_cost,
            pricing_version_id=rate.pricing_version_id if rate else None,
        ) as telemetry_attempt:
            reservation = None
            if self.session.scalar(
                select(ProjectBudget).where(ProjectBudget.project_id == project.id)
            ):
                reservation = CostRepository(self.session).reserve(
                    CostReservationRequest(
                        project_id=project.id,
                        provider_attempt_id=telemetry_attempt.row.id,
                        idempotency_key=f"{key}:reservation",
                        estimated_amount=estimated_cost,
                        currency="USD",
                    )
                )
                if reservation.decision in (
                    BudgetDecision.DENY_HARD_CAP,
                    BudgetDecision.DENY_ENTITY_CAP,
                ):
                    raise BudgetExceededError("narration request denied by project budget")
            result = await self.provider.generate(request, destination)
            provider_probe = probe_audio(destination)
            actual_cost = (
                Decimal(str(provider_probe.duration_seconds)) / rate.unit_size * rate.unit_price
                if rate
                else Decimal("0")
            )
            telemetry_attempt.set_result(
                provider_request_id=result.provider_request_id,
                usage=[
                    {"unit": unit, "quantity": quantity} for unit, quantity in result.usage.items()
                ],
                metadata=dict(result.response_metadata),
                actual_cost=actual_cost,
            )
            if reservation and reservation.reservation_id:
                CostRepository(self.session).reconcile(
                    reservation.reservation_id, f"{key}:reconciliation", actual_cost
                )
        return result

    async def _align_instrumented(
        self,
        *,
        row: NarrationSegment,
        project: Project,
        profile: VoiceProfileRecord,
        text: str,
        duration: float,
        path: Path,
    ) -> NarrationAlignment:
        project.status = "narration_aligning"
        key = f"{row.generation_identity}:alignment"
        if row.alignment is not None:
            return NarrationAlignment.model_validate(row.alignment)
        provider = "openai" if self.provider.name != "fake" else "fake"
        model = "whisper-1" if self.provider.name != "fake" else "fake-aligner"
        rate = self.session.scalar(
            select(ProviderPriceRate)
            .where(
                ProviderPriceRate.provider == provider,
                ProviderPriceRate.model == model,
                ProviderPriceRate.operation == "narration.align",
                ProviderPriceRate.usage_unit == "AUDIO_INPUT_SECOND",
                ProviderPriceRate.active,
            )
            .order_by(ProviderPriceRate.effective_start.desc())
        )
        cost = Decimal(str(duration)) / rate.unit_size * rate.unit_price if rate else Decimal("0")
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=project.id,
            provider=provider,
            model=model,
            operation="narration.align",
            input_hash=row.generation_identity,
            idempotency_key=key,
            related_entity_id=row.id,
            estimated_cost=cost,
            pricing_version_id=rate.pricing_version_id if rate else None,
        ) as telemetry_attempt:
            reservation = None
            if self.session.scalar(
                select(ProjectBudget).where(ProjectBudget.project_id == project.id)
            ):
                reservation = CostRepository(self.session).reserve(
                    CostReservationRequest(
                        project_id=project.id,
                        provider_attempt_id=telemetry_attempt.row.id,
                        idempotency_key=f"{key}:reservation",
                        estimated_amount=cost,
                        currency="USD",
                    )
                )
                if reservation.decision in (
                    BudgetDecision.DENY_HARD_CAP,
                    BudgetDecision.DENY_ENTITY_CAP,
                ):
                    raise BudgetExceededError("alignment request denied by project budget")
            alignment = self.aligner.align(text, duration, path, idempotency_key=key)
            row.alignment = alignment.model_dump(mode="json")
            self.session.commit()  # durable alignment result checkpoint
            telemetry_attempt.set_result(
                usage=[{"unit": "AUDIO_INPUT_SECOND", "quantity": duration}],
                metadata={"coverage": alignment.coverage},
                actual_cost=cost,
            )
            if reservation and reservation.reservation_id:
                CostRepository(self.session).reconcile(
                    reservation.reservation_id, f"{key}:reconciliation", cost
                )
        return alignment

    @staticmethod
    def _retry_instructions(previous: list[NarrationAttemptRecord]) -> str:
        if not previous or not previous[-1].quality_result:
            return ""
        codes = {item.get("code") for item in previous[-1].quality_result.get("diagnostics", [])}
        guidance: list[str] = []
        if "alignment_coverage" in codes:
            guidance.append("Pronounce every approved word exactly and clearly.")
        if "speaking_rate" in codes:
            guidance.append("Adjust delivery pace toward a natural 150 words per minute.")
        if codes & {"clipping", "leading_silence", "trailing_silence"}:
            guidance.append("Avoid clipping and begin and end promptly without silence.")
        return " ".join(guidance)

    def _result(self, run: NarrationRun, project_id: UUID) -> NarrationResult:
        rows = list(
            self.session.query(NarrationSegment)
            .filter_by(narration_run_id=run.id)
            .order_by(NarrationSegment.sequence)
        )
        segments: list[NarrationSegmentResult] = []
        for r in rows:
            if r.status != "complete" or None in (
                r.normalized_asset_id,
                r.duration_seconds,
                r.alignment,
                r.quality_report,
            ):
                continue
            assert r.normalized_asset_id is not None
            assert r.duration_seconds is not None
            assert r.alignment is not None
            assert r.quality_report is not None
            asset = self.session.get(Asset, r.normalized_asset_id)
            if asset is None:
                raise ValueError("completed narration segment asset is missing")
            segments.append(
                NarrationSegmentResult(
                    script_segment_id=r.script_segment_id,
                    sequence=r.sequence,
                    generation_identity=r.generation_identity,
                    normalized_asset_id=r.normalized_asset_id,
                    duration_seconds=r.duration_seconds,
                    audio_sha256=asset.sha256,
                    alignment=NarrationAlignment.model_validate(r.alignment),
                    quality_report=NarrationQualityReport.model_validate(r.quality_report),
                    selected_attempt_id=r.selected_attempt_id,
                    reused_from_segment_id=r.reused_from_segment_id,
                )
            )
        if run.status not in ("narration_complete", "narration_failed"):
            raise ValueError("narration result requested before terminal status")
        status: Literal["narration_complete", "narration_failed"] = (
            "narration_complete" if run.status == "narration_complete" else "narration_failed"
        )
        return NarrationResult(
            narration_run_id=run.id,
            project_id=project_id,
            status=status,
            segments=segments,
            preview_manifest_asset_id=UUID(run.parameters["manifest_asset_id"])
            if "manifest_asset_id" in run.parameters
            else None,
        )
