"""Restartable manifest-only T17 rendering pipeline.

The pipeline intentionally receives an immutable :class:`RenderManifest` and an
asset resolver. It never performs an upstream "latest" query after rendering
starts. Durable stores provide completed-identity lookup and immutable output
persistence; the filesystem implementation is the deterministic test adapter.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from services.renderer.audio import parse_loudnorm_json
from services.renderer.captions import serialize_srt, serialize_webvtt
from services.renderer.commands import build_command_plan
from services.renderer.manifest import canonical_json, reproducibility_hash
from services.renderer.render import CommandExecutor, contained
from services.renderer.verify import decode_complete, diagnostic_intervals, probe, verify_streams
from vidgen.contracts.render import CaptionTrack, RenderManifest
from vidgen.db.models import Asset, RenderJob
from vidgen.db.render_repository import RenderRepository
from vidgen.storage.asset_service import AssetService

AssetResolver = Callable[[UUID, Path], None]


@dataclass(frozen=True, slots=True)
class PersistedArtifact:
    asset_id: UUID
    sha256: str
    media_type: str
    path: Path


@dataclass(frozen=True, slots=True)
class CompletedRender:
    render_identity: str
    manifest: PersistedArtifact
    srt: PersistedArtifact
    webvtt: PersistedArtifact
    final_video: PersistedArtifact
    verification_report: PersistedArtifact
    measured_duration_us: int
    premaster_audio: PersistedArtifact | None = None
    normalized_audio: PersistedArtifact | None = None
    picture_master: PersistedArtifact | None = None
    reused: bool = False


class ArtifactStore(Protocol):
    def store_bytes(
        self, *, content: bytes, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact: ...

    def store_file(
        self, *, source: Path, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact: ...

    def completed(self, render_identity: str) -> CompletedRender | None: ...

    def mark_completed(self, result: CompletedRender) -> None: ...


class FilesystemArtifactStore:
    """Content-addressed immutable test/development store with a durable index."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.objects = self.root / "objects"
        self.index = self.root / "completed"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.index.mkdir(parents=True, exist_ok=True)

    def _artifact(
        self, *, digest: str, media_type: str, source: Path | None, content: bytes | None
    ) -> PersistedArtifact:
        destination = self.objects / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".partial")
            if source is not None:
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            else:
                assert content is not None
                temporary.write_bytes(content)
            try:
                temporary.replace(destination)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
        return PersistedArtifact(
            asset_id=uuid5(NAMESPACE_URL, f"vidgen-asset:{digest}"),
            sha256=digest,
            media_type=media_type,
            path=destination,
        )

    def store_bytes(
        self, *, content: bytes, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact:
        del kind, identity_key
        return self._artifact(
            digest=hashlib.sha256(content).hexdigest(),
            media_type=media_type,
            source=None,
            content=content,
        )

    def store_file(
        self, *, source: Path, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact:
        del kind, identity_key
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return self._artifact(
            digest=digest.hexdigest(), media_type=media_type, source=source, content=None
        )

    def completed(self, render_identity: str) -> CompletedRender | None:
        record = self.index / f"{render_identity}.json"
        if not record.exists():
            return None
        value = json.loads(record.read_text(encoding="utf-8"))

        def artifact(name: str) -> PersistedArtifact:
            item = value[name]
            return PersistedArtifact(
                asset_id=UUID(item["asset_id"]),
                sha256=item["sha256"],
                media_type=item["media_type"],
                path=self.objects / item["sha256"][:2] / item["sha256"],
            )

        return CompletedRender(
            render_identity=render_identity,
            manifest=artifact("manifest"),
            srt=artifact("srt"),
            webvtt=artifact("webvtt"),
            final_video=artifact("final_video"),
            verification_report=artifact("verification_report"),
            measured_duration_us=int(value["measured_duration_us"]),
            reused=True,
        )

    def mark_completed(self, result: CompletedRender) -> None:
        def artifact(value: PersistedArtifact) -> dict[str, str]:
            return {
                "asset_id": str(value.asset_id),
                "sha256": value.sha256,
                "media_type": value.media_type,
            }

        payload = {
            "render_identity": result.render_identity,
            "manifest": artifact(result.manifest),
            "srt": artifact(result.srt),
            "webvtt": artifact(result.webvtt),
            "final_video": artifact(result.final_video),
            "verification_report": artifact(result.verification_report),
            "measured_duration_us": result.measured_duration_us,
        }
        target = self.index / f"{result.render_identity}.json"
        temporary = target.with_suffix(".partial")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        try:
            temporary.replace(target)
        except FileExistsError:
            temporary.unlink(missing_ok=True)


class AssetServiceArtifactStore:
    """Production adapter persisting every output through immutable AssetService."""

    def __init__(self, service: AssetService, job: RenderJob, cache_root: Path) -> None:
        self.service = service
        self.job = job
        self.cache_root = cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _artifact(self, asset_id: UUID) -> PersistedArtifact:
        record = self.service.session.get(Asset, asset_id)
        if record is None:
            raise ValueError("persisted render asset is missing")
        path = self.cache_root / record.sha256
        if not path.exists():
            self.service.blob_store.copy_to(record.storage_key, path)
        return PersistedArtifact(
            asset_id=record.id,
            sha256=record.sha256,
            media_type=record.media_type,
            path=path,
        )

    def store_bytes(
        self, *, content: bytes, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact:
        parent_ids = tuple(
            asset_id
            for asset_id in (
                self.job.manifest_asset_id,
                self.job.srt_asset_id,
                self.job.webvtt_asset_id,
                self.job.final_video_asset_id,
            )
            if asset_id is not None
        )
        stored = self.service.store(
            content=content,
            kind=kind,
            media_type=media_type,
            project_id=self.job.project_id,
            parent_asset_ids=parent_ids,
            idempotency_key=identity_key,
            metadata={
                "render_job_id": str(self.job.id),
                "render_identity": self.job.render_identity,
                "pipeline_version": "t17/1",
            },
        )
        if kind == "render_manifest":
            self.job.manifest_asset_id = stored.id
        elif kind == "caption_srt":
            self.job.srt_asset_id = stored.id
        elif kind == "caption_webvtt":
            self.job.webvtt_asset_id = stored.id
        elif kind == "render_verification":
            self.job.verification_report_asset_id = stored.id
        self.service.session.flush()
        return self._artifact(stored.id)

    def store_file(
        self, *, source: Path, media_type: str, kind: str, identity_key: str
    ) -> PersistedArtifact:
        parent_ids = tuple(
            asset_id
            for asset_id in (
                self.job.manifest_asset_id,
                self.job.srt_asset_id,
                self.job.webvtt_asset_id,
            )
            if asset_id is not None
        )
        stored = self.service.store_file(
            path=source,
            kind=kind,
            media_type=media_type,
            project_id=self.job.project_id,
            parent_asset_ids=parent_ids,
            idempotency_key=identity_key,
            metadata={
                "render_job_id": str(self.job.id),
                "render_identity": self.job.render_identity,
                "pipeline_version": "t17/1",
            },
        )
        if kind == "final_render":
            self.job.final_video_asset_id = stored.id
            self.job.output_asset_id = stored.id
        elif kind == "render_audio_premaster":
            self.job.premaster_audio_asset_id = stored.id
        self.service.session.flush()
        return self._artifact(stored.id)

    def completed(self, render_identity: str) -> CompletedRender | None:
        job = RenderRepository(self.service.session).completed_by_identity(render_identity)
        if job is None:
            return None
        required = (
            job.manifest_asset_id,
            job.srt_asset_id,
            job.webvtt_asset_id,
            job.final_video_asset_id,
            job.verification_report_asset_id,
        )
        if any(asset_id is None for asset_id in required):
            return None
        manifest, srt, webvtt, final_video, report = (
            self._artifact(asset_id) for asset_id in required if asset_id is not None
        )
        return CompletedRender(
            render_identity=render_identity,
            manifest=manifest,
            srt=srt,
            webvtt=webvtt,
            final_video=final_video,
            verification_report=report,
            measured_duration_us=job.measured_duration_us or 0,
            reused=True,
        )

    def mark_completed(self, result: CompletedRender) -> None:
        self.job.manifest_asset_id = result.manifest.asset_id
        self.job.srt_asset_id = result.srt.asset_id
        self.job.webvtt_asset_id = result.webvtt.asset_id
        self.job.final_video_asset_id = result.final_video.asset_id
        self.job.output_asset_id = result.final_video.asset_id
        self.job.verification_report_asset_id = result.verification_report.asset_id
        self.job.measured_duration_us = result.measured_duration_us
        self.job.status = "render_complete"
        self.job.completed_at = datetime.now(UTC)
        self.service.session.flush()


class DeterministicRenderPipeline:
    def __init__(
        self,
        *,
        store: ArtifactStore,
        work_root: Path,
        executor: CommandExecutor | None = None,
        preserve_failed_attempts: bool = False,
    ) -> None:
        self.store = store
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.executor = executor or CommandExecutor()
        self.preserve_failed_attempts = preserve_failed_attempts

    def run(
        self,
        *,
        manifest: RenderManifest,
        caption_track: CaptionTrack,
        resolve_asset: AssetResolver,
    ) -> CompletedRender:
        existing = self.store.completed(manifest.render_identity)
        if existing is not None:
            return existing
        if caption_track.caption_track_id != manifest.caption_track_id:
            raise ValueError("caption track does not match immutable manifest")

        attempt = Path(
            tempfile.mkdtemp(prefix=f"render-{manifest.render_identity[:12]}-", dir=self.work_root)
        ).resolve()
        contained(self.work_root, attempt)
        succeeded = False
        try:
            self._stage_inputs(manifest, attempt, resolve_asset)
            srt_text = serialize_srt(caption_track)
            webvtt_text = serialize_webvtt(caption_track)
            (attempt / "captions.srt").write_text(srt_text, encoding="utf-8", newline="\n")
            (attempt / "captions.vtt").write_text(webvtt_text, encoding="utf-8", newline="\n")
            plan = build_command_plan(manifest, attempt)
            self._write_concat(manifest, attempt)
            for index, arguments in enumerate(plan.normalization_arguments):
                self.executor.run(arguments, f"normalize:{index}")
            self.executor.run(plan.picture_arguments, "picture")
            self.executor.run(plan.premaster_arguments, "premaster")
            pass1 = self.executor.run(plan.loudness_pass1_arguments, "loudness-measure")
            loudness = parse_loudnorm_json(pass1.stderr.decode("utf-8", "replace"))
            second_pass = self._measured_loudness_arguments(plan.loudness_pass2_arguments, loudness)
            self.executor.run(second_pass, "loudness-normalize")
            self.executor.run(plan.final_arguments, "encode")

            final_path = attempt / "final.mp4"
            metadata = probe(final_path)
            streams = verify_streams(
                metadata,
                fps=manifest.video_profile.frame_rate,
                duration_us=manifest.narration_duration_us,
            )
            decode_complete(final_path)
            black_intervals = diagnostic_intervals(final_path, "black")
            freeze_intervals = diagnostic_intervals(final_path, "freeze")
            silence_intervals = diagnostic_intervals(final_path, "silence")
            manifest_asset = self.store.store_bytes(
                content=canonical_json(manifest),
                media_type="application/vnd.vidgen.render-manifest+json",
                kind="render_manifest",
                identity_key=f"{manifest.render_identity}:manifest",
            )
            srt_asset = self.store.store_bytes(
                content=srt_text.encode(),
                media_type="application/x-subrip",
                kind="caption_srt",
                identity_key=f"{manifest.render_identity}:srt",
            )
            webvtt_asset = self.store.store_bytes(
                content=webvtt_text.encode(),
                media_type="text/vtt",
                kind="caption_webvtt",
                identity_key=f"{manifest.render_identity}:webvtt",
            )
            final_asset = self.store.store_file(
                source=final_path,
                media_type="video/mp4",
                kind="final_render",
                identity_key=f"{manifest.render_identity}:final",
            )
            premaster_asset = self.store.store_file(
                source=attempt / "premaster.wav",
                media_type="audio/wav",
                kind="render_audio_premaster",
                identity_key=f"{manifest.render_identity}:premaster",
            )
            normalized_audio_asset = self.store.store_file(
                source=attempt / "master.wav",
                media_type="audio/wav",
                kind="render_audio_master",
                identity_key=f"{manifest.render_identity}:audio-master",
            )
            picture_asset = self.store.store_file(
                source=attempt / "picture.mp4",
                media_type="video/mp4",
                kind="render_picture_master",
                identity_key=f"{manifest.render_identity}:picture-master",
            )
            measured = int(streams["measured_duration_us"])
            report_payload: dict[str, Any] = {
                "schema_version": "1.0",
                "render_identity": manifest.render_identity,
                "manifest_hash": manifest_asset.sha256,
                "final_video_hash": final_asset.sha256,
                "caption_hashes": [srt_asset.sha256, webvtt_asset.sha256],
                "command_plan_hash": plan.command_plan_hash,
                "expected_duration_us": manifest.narration_duration_us,
                "measured_duration_us": measured,
                "duration_difference_us": measured - manifest.narration_duration_us,
                "stream_metadata": streams,
                "loudness": loudness,
                "full_decode_ok": True,
                "subtitle_valid": True,
                "black_intervals": black_intervals,
                "freeze_intervals": freeze_intervals,
                "silence_intervals": silence_intervals,
                "verification_profile_version": manifest.verification_profile_version,
            }
            report_payload["reproducibility_hash"] = reproducibility_hash(report_payload)
            report_asset = self.store.store_bytes(
                content=json.dumps(report_payload, sort_keys=True, separators=(",", ":")).encode(),
                media_type="application/vnd.vidgen.render-verification+json",
                kind="render_verification",
                identity_key=f"{manifest.render_identity}:verification",
            )
            result = CompletedRender(
                render_identity=manifest.render_identity,
                manifest=manifest_asset,
                srt=srt_asset,
                webvtt=webvtt_asset,
                final_video=final_asset,
                verification_report=report_asset,
                measured_duration_us=measured,
                premaster_audio=premaster_asset,
                normalized_audio=normalized_audio_asset,
                picture_master=picture_asset,
            )
            self.store.mark_completed(result)
            succeeded = True
            return result
        finally:
            if succeeded or not self.preserve_failed_attempts:
                shutil.rmtree(attempt, ignore_errors=True)

    @staticmethod
    def _stage_inputs(
        manifest: RenderManifest, attempt: Path, resolve_asset: AssetResolver
    ) -> None:
        references = [shot.video for shot in manifest.shots]
        references.extend(entry.asset for entry in manifest.audio_entries)
        if len(references) > 628:
            raise ValueError("render input count exceeds configured cap")
        total = 0
        for reference in references:
            destination = contained(attempt, attempt / f"{reference.sha256}.input")
            if not destination.exists():
                resolve_asset(reference.asset_id, destination)
            digest = hashlib.sha256()
            size = 0
            with destination.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 2_000_000_000:
                        raise ValueError("individual staged input exceeds size cap")
                    digest.update(chunk)
            total += size
            if total > 20_000_000_000:
                raise ValueError("total staged input exceeds size cap")
            if digest.hexdigest() != reference.sha256:
                raise ValueError("staged asset hash mismatch")

    @staticmethod
    def _write_concat(manifest: RenderManifest, attempt: Path) -> None:
        entries = []
        for shot in manifest.shots:
            path = contained(attempt, attempt / f"shot-{shot.sequence:04}.mp4")
            entries.append(f"file '{path.as_posix().replace(chr(39), "'\\''")}'")
        (attempt / "concat.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")

    @staticmethod
    def _measured_loudness_arguments(
        arguments: list[str], measurements: dict[str, float]
    ) -> list[str]:
        result = list(arguments)
        index = result.index("-af") + 1
        result[index] += (
            f":measured_I={measurements['integrated_lufs']}"
            f":measured_TP={measurements['true_peak_dbtp']}"
            f":measured_LRA={measurements['loudness_range']}"
            f":measured_thresh={measurements['threshold']}"
            f":offset={measurements['offset']}:linear=true"
        )
        return result
