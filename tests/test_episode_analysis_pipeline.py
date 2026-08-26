from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.pipeline import EpisodeAnalysisPipeline
from vidgen.db.base import Base
from vidgen.db.episode_analysis_models import (
    EpisodeAnalysisRecord,
    EpisodeAnalysisRun,
    SceneAnalysisCheckpoint,
)
from vidgen.db.models import Asset, Project, Scene, SourceVideo
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def _database(
    tmp_path: Path,
) -> tuple[Session, FilesystemBlobStore, Project, EvidencePackageRecord]:
    url = f"sqlite:///{tmp_path / 'analysis.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    blobs = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    project = Project(name="test", visual_style="flat", status="evidence_ready")
    session.add(project)
    session.flush()
    assets = AssetService(session, blobs)
    source_asset = assets.store(
        content=b"video", kind="source_video", media_type="video/mp4", project_id=project.id
    )
    transcript_asset = assets.store(
        content=b"text", kind="json", media_type="application/json", project_id=project.id
    )
    frame = assets.store(
        content=b"frame", kind="frame", media_type="image/png", project_id=project.id
    )
    package_asset = assets.store(
        content=b"package",
        kind="json",
        media_type="application/json",
        project_id=project.id,
        parent_asset_ids=(source_asset.id, transcript_asset.id, frame.id),
    )
    source = SourceVideo(
        project_id=project.id, asset_id=source_asset.id, filename="test.mp4", duration_seconds=2
    )
    session.add(source)
    session.flush()
    session.add_all(
        Scene(
            project_id=project.id,
            sequence=i,
            source_start_seconds=float(i),
            source_end_seconds=float(i + 1),
            summary="pending",
        )
        for i in range(2)
    )
    evidence = EvidencePackageRecord(
        project_id=project.id,
        version=1,
        selected=True,
        input_hash="a" * 64,
        schema_version="1.0",
        source_video_id=source.id,
        source_video_asset_id=source_asset.id,
        transcript_id=uuid4(),
        transcript_asset_id=transcript_asset.id,
        transcript_origin="subtitle",
        provenance={"package_asset_id": str(package_asset.id)},
    )
    session.add(evidence)
    session.flush()
    session.add_all(
        [
            SceneEvidenceRecord(
                evidence_package_id=evidence.id,
                scene_sequence=i,
                source_start_seconds=float(i),
                source_end_seconds=float(i + 1),
                frame_asset_ids=[str(frame.id)],
                evidence={"transcript_items": []},
            )
            for i in range(2)
        ]
    )
    session.commit()
    return session, blobs, project, evidence


@pytest.mark.asyncio
async def test_pipeline_checkpoints_assets_and_reuses_completed_run(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    provider = FakeEpisodeAnalysisProvider()
    pipeline = EpisodeAnalysisPipeline(session, blobs, provider)
    first = await pipeline.process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="analysis-key"
    )
    calls = list(provider.submissions)
    second = await pipeline.process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="analysis-key"
    )
    assert second.episode_analysis_id == first.episode_analysis_id
    assert provider.submissions == calls
    assert (
        session.scalar(
            select(EpisodeAnalysisRun).where(EpisodeAnalysisRun.project_id == project.id)
        )
        is not None
    )
    assert len(list(session.scalars(select(SceneAnalysisCheckpoint)))) == 2
    analysis = session.get(EpisodeAnalysisRecord, first.episode_analysis_id)
    assert analysis is not None and analysis.selected and project.status == "episode_analyzed"
    asset = session.get(Asset, analysis.canonical_analysis_asset_id)
    assert asset is not None and asset.parents


@pytest.mark.asyncio
async def test_rejects_unselected_evidence(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    evidence.selected = False
    session.commit()
    with pytest.raises(ValueError, match="unselected"):
        await EpisodeAnalysisPipeline(session, blobs, FakeEpisodeAnalysisProvider()).process(
            project_id=project.id, evidence_package_id=evidence.id, idempotency_key="x"
        )


@pytest.mark.asyncio
async def test_interrupted_reduce_reuses_scene_checkpoints(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)

    class FailingReduce(FakeEpisodeAnalysisProvider):
        async def synthesize_episode(self, request, context):  # type: ignore[no-untyped-def]
            raise TimeoutError("interrupted")

    with pytest.raises(TimeoutError):
        await EpisodeAnalysisPipeline(session, blobs, FailingReduce()).process(
            project_id=project.id,
            evidence_package_id=evidence.id,
            idempotency_key="resume-key",
        )
    assert project.status == "episode_analysis_failed"
    resumed_provider = FakeEpisodeAnalysisProvider()
    result = await EpisodeAnalysisPipeline(session, blobs, resumed_provider).process(
        project_id=project.id,
        evidence_package_id=evidence.id,
        idempotency_key="resume-key",
    )
    assert result.validation_report.valid
    assert resumed_provider.submissions == ["resume-key:reduce"]


@pytest.mark.asyncio
async def test_failed_replacement_preserves_selected_analysis(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    first = await EpisodeAnalysisPipeline(session, blobs, FakeEpisodeAnalysisProvider()).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="first"
    )
    evidence.selected = False
    replacement_asset = AssetService(session, blobs).store(
        content=b"replacement", kind="json", media_type="application/json", project_id=project.id
    )
    replacement = EvidencePackageRecord(
        project_id=project.id,
        version=2,
        selected=True,
        input_hash="b" * 64,
        schema_version="1.0",
        source_video_id=evidence.source_video_id,
        source_video_asset_id=evidence.source_video_asset_id,
        transcript_id=uuid4(),
        transcript_asset_id=evidence.transcript_asset_id,
        transcript_origin="subtitle",
        provenance={"package_asset_id": str(replacement_asset.id)},
    )
    session.add(replacement)
    session.flush()
    original_scenes = list(
        session.scalars(
            select(SceneEvidenceRecord).where(
                SceneEvidenceRecord.evidence_package_id == evidence.id
            )
        )
    )
    session.add_all(
        SceneEvidenceRecord(
            evidence_package_id=replacement.id,
            scene_sequence=item.scene_sequence,
            source_start_seconds=item.source_start_seconds,
            source_end_seconds=item.source_end_seconds,
            frame_asset_ids=item.frame_asset_ids,
            evidence=item.evidence,
        )
        for item in original_scenes
    )
    session.commit()

    class FailingReduce(FakeEpisodeAnalysisProvider):
        async def synthesize_episode(self, request, context):  # type: ignore[no-untyped-def]
            raise TimeoutError("replacement failed")

    with pytest.raises(TimeoutError):
        await EpisodeAnalysisPipeline(session, blobs, FailingReduce()).process(
            project_id=project.id, evidence_package_id=replacement.id, idempotency_key="replacement"
        )
    selected = session.scalar(
        select(EpisodeAnalysisRecord).where(
            EpisodeAnalysisRecord.project_id == project.id, EpisodeAnalysisRecord.selected
        )
    )
    assert selected is not None and selected.id == first.episode_analysis_id


@pytest.mark.asyncio
async def test_failed_scene_retry_does_not_rerun_successful_scene(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)

    class InvalidSecondScene(FakeEpisodeAnalysisProvider):
        async def analyze_scene(self, request, context):  # type: ignore[no-untyped-def]
            result = await super().analyze_scene(request, context)
            if request.sequence == 2:
                result.output.source_end_ms += 1
            return result

    with pytest.raises(RuntimeError, match="scene attempts exhausted"):
        await EpisodeAnalysisPipeline(session, blobs, InvalidSecondScene()).process(
            project_id=project.id,
            evidence_package_id=evidence.id,
            idempotency_key="scene-recovery",
        )
    checkpoints = list(session.scalars(select(SceneAnalysisCheckpoint)))
    assert {item.sequence: item.status for item in checkpoints} == {1: "succeeded", 2: "invalid"}
    resumed = FakeEpisodeAnalysisProvider()
    result = await EpisodeAnalysisPipeline(session, blobs, resumed).process(
        project_id=project.id,
        evidence_package_id=evidence.id,
        idempotency_key="scene-recovery",
    )
    assert result.validation_report.valid
    assert len([key for key in resumed.submissions if ":scene:" in key]) == 1
