from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import vidgen.db.workflow_models  # noqa: F401
from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.pipeline import ScriptGenerationPipeline
from vidgen.contracts.episode_analysis import (
    BeatDependency,
    CanonicalScene,
    CharacterCandidate,
    EpisodeAnalysis,
    PlotBeat,
    SourceReference,
)
from vidgen.db.base import Base
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord, EpisodeAnalysisRun
from vidgen.db.models import Project
from vidgen.db.script_models import Script, ScriptGenerationRun, ScriptSegment
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def _make_analysis(project_id, beat_count: int = 15) -> EpisodeAnalysis:
    episode_id = uuid4()
    source_video_id = uuid4()
    evidence_package_id = uuid4()
    char_ids = [uuid4() for _ in range(3)]
    scene_ids = [uuid4() for _ in range(beat_count)]
    ref = SourceReference(reference_type="project", reference_id=project_id)
    characters = [
        CharacterCandidate(
            character_id=cid,
            canonical_name=f"Character {i}",
            confidence=1.0,
            source_references=[ref],
        )
        for i, cid in enumerate(char_ids)
    ]
    scenes = [
        CanonicalScene(
            scene_id=sid,
            sequence=i + 1,
            source_start_ms=i * 1000,
            source_end_ms=i * 1000 + 900,
            summary=f"Scene {i} summary",
            dramatic_purpose="advance plot",
            confidence=1.0,
            source_references=[ref],
        )
        for i, sid in enumerate(scene_ids)
    ]
    beat_ids = [uuid4() for _ in range(beat_count)]
    beats = [
        PlotBeat(
            plot_beat_id=bid,
            sequence=i + 1,
            scene_ids=[scene_ids[i]],
            character_ids=char_ids[:1],
            summary=f"Beat {i}: something happens that matters a lot to the plot",
            importance=0.5 + (i % 3) * 0.1,
            payoff_score=0.4 + (i % 4) * 0.15,
            mandatory=(i in (0, beat_count // 2, beat_count - 1)),
            source_references=[ref],
        )
        for i, bid in enumerate(beat_ids)
    ]
    deps = [
        BeatDependency(
            cause_beat_id=beat_ids[i], effect_beat_id=beat_ids[i + 1], source_references=[ref]
        )
        for i in range(len(beat_ids) - 1)
    ]
    return EpisodeAnalysis(
        episode_id=episode_id,
        project_id=project_id,
        source_video_id=source_video_id,
        evidence_package_id=evidence_package_id,
        duration_ms=900_000,
        characters=characters,
        scenes=scenes,
        plot_beats=beats,
        beat_dependencies=deps,
        source_references=[ref],
    )


def _database(
    tmp_path: Path, *, beat_count: int = 15, project_settings: dict | None = None
) -> tuple[Session, FilesystemBlobStore, Project, EpisodeAnalysisRecord]:
    url = f"sqlite:///{tmp_path / 'script.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    blobs = FilesystemBlobStore(tmp_path / "blobs", b"secret")
    project = Project(
        name="test",
        visual_style="flat",
        status="episode_analyzed",
        target_duration_seconds=240,
        settings=project_settings or {},
    )
    session.add(project)
    session.flush()
    analysis = _make_analysis(project.id, beat_count=beat_count)
    assets = AssetService(session, blobs)
    analysis_asset = assets.store(
        content=analysis.model_dump_json().encode(),
        kind="json",
        media_type="application/vnd.vidgen.episode-analysis+json",
        project_id=project.id,
    )
    run = EpisodeAnalysisRun(
        project_id=project.id,
        source_video_id=analysis.source_video_id,
        evidence_package_id=analysis.evidence_package_id,
        idempotency_key="analysis-key",
        input_hash="a" * 64,
        contract_version="1.0",
        prompt_version="episode-analysis-v1",
        provider_configuration_version="fake-episode-v1",
        provider="fake",
        model="deterministic-episode-v1",
        status="episode_analyzed",
        attempt_count=1,
        selected=True,
    )
    session.add(run)
    session.flush()
    record = EpisodeAnalysisRecord(
        project_id=project.id,
        analysis_run_id=run.id,
        version=1,
        canonical_analysis_asset_id=analysis_asset.id,
        input_hash="a" * 64,
        duration_ms=analysis.duration_ms,
        character_count=len(analysis.characters),
        location_count=0,
        scene_count=len(analysis.scenes),
        plot_beat_count=len(analysis.plot_beats),
        selected=True,
        warnings=[],
    )
    session.add(record)
    session.commit()
    return session, blobs, project, record


@pytest.mark.asyncio
async def test_pipeline_produces_an_approved_script(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_approved"
    assert result.script_id is not None
    assert result.review_scores is not None
    assert result.review_scores.overall >= 85
    assert result.review_scores.plot_fidelity >= 92

    session.expire_all()
    script_record = session.get(Script, result.script_id)
    assert script_record is not None
    assert script_record.selected is True
    assert (
        abs(script_record.actual_word_count - script_record.target_word_count)
        / script_record.target_word_count
        <= 0.05
    )

    segments = session.scalars(
        select(ScriptSegment).where(ScriptSegment.script_id == script_record.id)
    ).all()
    assert len(segments) > 0

    project_row = session.get(Project, project.id)
    assert project_row is not None and project_row.status == "script_approved"


@pytest.mark.asyncio
async def test_completed_run_is_idempotent(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    first = await pipeline.process(project_id=project.id, idempotency_key="run-1")
    submissions_after_first = len(provider.submissions)
    second = await pipeline.process(project_id=project.id, idempotency_key="run-1")
    assert second.script_id == first.script_id
    assert second.script_version == first.script_version
    assert len(provider.submissions) == submissions_after_first

    runs = session.scalars(select(ScriptGenerationRun)).all()
    assert len(runs) == 1
    scripts = session.scalars(select(Script)).all()
    assert len({s.id for s in scripts}) == len(scripts)


@pytest.mark.asyncio
async def test_missing_selected_analysis_is_rejected(tmp_path: Path) -> None:
    session, blobs, project, record = _database(tmp_path)
    record.selected = False
    session.commit()
    provider = FakeScriptGenerationProvider()
    with pytest.raises(ValueError, match="no selected T10 episode analysis"):
        await ScriptGenerationPipeline(session, blobs, provider).process(
            project_id=project.id, idempotency_key="run-1"
        )
    session.expire_all()
    assert session.get(Project, project.id).status == "script_generation_failed"


@pytest.mark.asyncio
async def test_missing_analysis_asset_is_rejected(tmp_path: Path) -> None:
    session, blobs, project, record = _database(tmp_path)
    (blobs.root / _asset_key(session, record)).unlink()
    provider = FakeScriptGenerationProvider()
    with pytest.raises(ValueError, match="canonical episode analysis asset is missing"):
        await ScriptGenerationPipeline(session, blobs, provider).process(
            project_id=project.id, idempotency_key="run-1"
        )


def _asset_key(session: Session, record: EpisodeAnalysisRecord) -> str:
    from vidgen.db.models import Asset

    asset = session.get(Asset, record.canonical_analysis_asset_id)
    assert asset is not None
    return asset.storage_key


@pytest.mark.asyncio
async def test_unresolvable_required_beat_id_is_rejected(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(
        tmp_path, project_settings={"script": {"required_beat_ids": [str(uuid4())]}}
    )
    provider = FakeScriptGenerationProvider()
    with pytest.raises(ValueError, match="required beat IDs do not resolve"):
        await ScriptGenerationPipeline(session, blobs, provider).process(
            project_id=project.id, idempotency_key="run-1"
        )


@pytest.mark.asyncio
async def test_target_words_outside_bounds_is_rejected(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(
        tmp_path, project_settings={"script": {"target_words": 10}}
    )
    provider = FakeScriptGenerationProvider()
    with pytest.raises(ValueError, match="outside the configured bounds"):
        await ScriptGenerationPipeline(session, blobs, provider).process(
            project_id=project.id, idempotency_key="run-1"
        )


@pytest.mark.asyncio
async def test_reusing_key_with_changed_analysis_requires_new_key(tmp_path: Path) -> None:
    session, blobs, project, record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    await pipeline.process(project_id=project.id, idempotency_key="run-1")

    # Simulate a changed T10 analysis by creating a new selected analysis version.
    session.expire_all()
    old_record = session.get(EpisodeAnalysisRecord, record.id)
    old_record.selected = False
    new_analysis = _make_analysis(project.id, beat_count=16)
    assets = AssetService(session, blobs)
    new_asset = assets.store(
        content=new_analysis.model_dump_json().encode(),
        kind="json",
        media_type="application/vnd.vidgen.episode-analysis+json",
        project_id=project.id,
    )
    new_run = EpisodeAnalysisRun(
        project_id=project.id,
        source_video_id=new_analysis.source_video_id,
        evidence_package_id=new_analysis.evidence_package_id,
        idempotency_key="analysis-key-2",
        input_hash="b" * 64,
        contract_version="1.0",
        prompt_version="episode-analysis-v1",
        provider_configuration_version="fake-episode-v1",
        provider="fake",
        model="deterministic-episode-v1",
        status="episode_analyzed",
        attempt_count=1,
        selected=True,
    )
    session.add(new_run)
    session.flush()
    new_record = EpisodeAnalysisRecord(
        project_id=project.id,
        analysis_run_id=new_run.id,
        version=2,
        canonical_analysis_asset_id=new_asset.id,
        input_hash="b" * 64,
        duration_ms=new_analysis.duration_ms,
        character_count=len(new_analysis.characters),
        location_count=0,
        scene_count=len(new_analysis.scenes),
        plot_beat_count=len(new_analysis.plot_beats),
        selected=True,
        warnings=[],
    )
    session.add(new_record)
    session.commit()

    with pytest.raises(ValueError, match="idempotency key is bound to a different"):
        await pipeline.process(project_id=project.id, idempotency_key="run-1")

    # A fresh idempotency key starts a new lineage against the new analysis.
    second_result = await pipeline.process(project_id=project.id, idempotency_key="run-2")
    assert second_result.status == "script_approved"
    session.expire_all()
    selected_scripts = session.scalars(
        select(Script).where(Script.project_id == project.id, Script.selected)
    ).all()
    assert len(selected_scripts) == 1
    assert selected_scripts[0].id == second_result.script_id


class _NeverApprovingProvider(FakeScriptGenerationProvider):
    """Delegates compression/writing to the real fake logic but never approves edits."""

    async def edit_script(self, request, context):  # type: ignore[override]
        result = await super().edit_script(request, context)
        revised = result.output.revised_script.model_copy(
            update={
                "script_id": result.output.revised_script.script_id,
                "actual_word_count": result.output.revised_script.actual_word_count,
            }
        )
        scores = result.output.scores.model_copy(update={"overall": 40, "plot_fidelity": 40})
        from vidgen.contracts.script import ComedyEditResult

        forced = ComedyEditResult(
            scores=scores,
            issues=result.output.issues,
            edits=result.output.edits,
            revised_script=revised,
            approval_recommendation="revise",
        )
        return result.model_copy(update={"output": forced})


@pytest.mark.asyncio
async def test_revision_exhaustion_sets_script_review_required(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = _NeverApprovingProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_review_required"
    session.expire_all()
    run = session.scalars(select(ScriptGenerationRun)).one()
    assert run.error_code == "REVISION_EXHAUSTED"
    assert run.revision_count == 2
    script_versions = session.scalars(
        select(Script).where(Script.generation_run_id == run.id)
    ).all()
    # draft (v1) plus one revised candidate per evaluation (3 evaluations total)
    assert len(script_versions) == 4
    assert session.get(Project, project.id).status == "script_review_required"


@pytest.mark.asyncio
async def test_failed_replacement_preserves_prior_selected_script(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    first = await ScriptGenerationPipeline(session, blobs, FakeScriptGenerationProvider()).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert first.status == "script_approved"

    second = await ScriptGenerationPipeline(session, blobs, _NeverApprovingProvider()).process(
        project_id=project.id, idempotency_key="run-2"
    )
    assert second.status == "script_review_required"

    session.expire_all()
    selected = session.scalars(
        select(Script).where(Script.project_id == project.id, Script.selected)
    ).all()
    assert len(selected) == 1
    assert selected[0].id == first.script_id
