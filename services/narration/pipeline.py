"""Restartable segment-at-a-time T12 orchestration."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from services.narration.alignment import FakeAligner
from services.narration.normalization import normalize_audio, probe_audio
from services.narration.preview import concatenate_preview
from services.narration.providers import NarrationProvider
from services.narration.quality import QualityThresholds, validate_quality
from vidgen.contracts.narration import (
    NarrationAlignment,
    NarrationProviderRequest,
    NarrationQualityReport,
    NarrationResult,
    NarrationSegmentResult,
)
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
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.thresholds = thresholds
        self.repo = NarrationRepository(session)
        self.assets = AssetService(session, blob_store)

    async def process(
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
                row = self.repo.segment_by_identity(identity)
                if row is None:
                    row = NarrationSegment(
                        narration_run_id=run.id,
                        script_segment_id=source.id,
                        sequence=source.sequence,
                        text_hash=text_hash,
                        generation_identity=identity,
                        status="pending",
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
            manifest = {
                "schema_version": "1.0",
                "run_id": str(run.id),
                "script_id": str(script.id),
                "script_version": script.version,
                "segments": [
                    {
                        "id": str(r.script_segment_id),
                        "asset_id": str(r.normalized_asset_id),
                        "duration": r.duration_seconds,
                    }
                    for r in selected
                ],
                "preview_asset_id": str(preview.id),
                "duration": preview_probe.duration_seconds,
                "input_hash": canonical_hash([str(x) for x in parent_ids]),
                "output_hash": preview.sha256,
            }
            manifest_asset = self.assets.store(
                content=json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                kind="json",
                media_type="application/json",
                project_id=project_id,
                parent_asset_ids=(preview.id, *parent_ids),
                idempotency_key=f"{run.id}:preview-manifest",
            )
            run.preview_asset_id = preview.id
            run.total_duration_seconds = preview_probe.duration_seconds
            run.parameters = {**material, "manifest_asset_id": str(manifest_asset.id)}
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
        attempt_no = len(self.repo.attempts(row.id)) + 1
        if attempt_no > 3:
            raise RuntimeError("narration segment exhausted three attempts")
        key = f"{row.generation_identity}:{attempt_no}"
        attempt = NarrationAttemptRecord(
            narration_segment_id=row.id,
            attempt_number=attempt_no,
            provider=self.provider.name,
            model=str(profile.model),
            provider_idempotency_key=key,
            voice_settings=cfg,
            instructions=str(source.voice_direction),
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
            speaking_instructions=str(
                source.voice_direction or cfg.get("default_speaking_instructions", "")
            ),
            speed=float(cfg.get("default_pace", 1)),
            output_format=str(cfg.get("output_format", "wav")),
            language=str(profile.language),
            attempt_number=attempt_no,
        )
        result = await self.provider.generate(request, original)
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
        normalize_audio(original, normalized)
        probe = probe_audio(normalized)
        project.status = "narration_aligning"
        alignment = FakeAligner().align(text, probe.duration_seconds)
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
        attempt.provider_output_asset_id = original_asset.id
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
                r.selected_attempt_id,
            ):
                continue
            assert r.normalized_asset_id is not None
            assert r.duration_seconds is not None
            assert r.alignment is not None
            assert r.quality_report is not None
            assert r.selected_attempt_id is not None
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
