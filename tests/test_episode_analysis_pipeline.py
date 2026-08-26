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
