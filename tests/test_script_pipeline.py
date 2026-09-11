from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import vidgen.db.workflow_models  # noqa: F401
from services.script.fake_provider import FakeScriptGenerationProvider
from services.script.pipeline import ScriptEditingExhausted, ScriptGenerationPipeline
from vidgen.contracts.episode_analysis import (
    BeatDependency,
    CanonicalScene,
    CharacterCandidate,
    EpisodeAnalysis,
    PlotBeat,
    SourceReference,
)
from vidgen.contracts.script import RecapScript
from vidgen.db.base import Base
from vidgen.db.cost_models import PipelineFailureEvent, ProviderAttempt
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


def _pending_candidate(session: Session, run_id) -> Script:
    """The version the pipeline left in front of the reviewer."""
    session.expire_all()
    candidates = session.scalars(
        select(Script).where(Script.generation_run_id == run_id).order_by(Script.version)
    ).all()
    assert candidates, "the run left no script behind"
    latest = candidates[-1]
    assert latest.status == "pending_review"
    return latest


def _approve(session: Session, script: Script) -> None:
    """What ``POST /scripts/{id}:select`` does: the existing selected/approved pattern."""
    session.query(Script).filter_by(project_id=script.project_id, selected=True).update(
        {"selected": False}
    )
    script.selected = True
    script.status = "approved"
    run = session.get(ScriptGenerationRun, script.generation_run_id)
    assert run is not None
    run.status = "script_approved"
    project = session.get(Project, script.project_id)
    assert project is not None
    project.status = "script_approved"
    session.commit()


def _reject(session: Session, script: Script, reason: str) -> None:
    """What ``POST /scripts/{id}:reject`` does: the reason stays with the version."""
    script.status = "rejected"
    script.rejection_reason = reason
    session.commit()


@pytest.mark.asyncio
async def test_pipeline_stops_after_the_first_editing_pass_for_review(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    # Pass 1 is a checkpoint, not a verdict: no version is selected, the
    # workflow's review gate fires on the missing entity, and the editor ran
    # exactly once.
    assert result.status == "script_review_required"
    assert result.script_id is None
    assert len(provider.edit_requests) == 1
    assert provider.edit_requests[0].reviewer_feedback is None
    assert provider.edit_requests[0].attempt_number == 1

    session.expire_all()
    run = session.scalars(select(ScriptGenerationRun)).one()
    assert run.status == "script_review_required"
    assert run.error_code is None
    assert run.max_editing_passes == 3
    versions = session.scalars(
        select(Script).where(Script.generation_run_id == run.id).order_by(Script.version)
    ).all()
    assert [item.status for item in versions] == ["draft", "pending_review"]
    assert [item.editing_pass for item in versions] == [0, 1]
    assert not any(item.selected for item in versions)
    candidate = versions[-1]
    assert candidate.parent_script_id == versions[0].id
    assert candidate.review_scores is not None
    assert (
        abs(candidate.actual_word_count - candidate.target_word_count) / candidate.target_word_count
        <= 0.05
    )
    segments = session.scalars(
        select(ScriptSegment).where(ScriptSegment.script_id == candidate.id)
    ).all()
    assert len(segments) > 0
    project_row = session.get(Project, project.id)
    assert project_row is not None and project_row.status == "script_review_required"


@pytest.mark.asyncio
async def test_approving_after_pass_one_skips_the_remaining_passes(tmp_path: Path) -> None:
    """Integration: approve → the run is done, and a continuation spends nothing."""
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    first = await pipeline.process(project_id=project.id, idempotency_key="run-1")
    assert first.status == "script_review_required"
    run = session.scalars(select(ScriptGenerationRun)).one()
    candidate = _pending_candidate(session, run.id)
    _approve(session, candidate)

    submissions = len(provider.submissions)
    # The workflow continues from ``script_generation`` under a new key.
    second = await pipeline.process(project_id=project.id, idempotency_key="continue-1")
    assert second.status == "script_approved"
    assert second.script_id == candidate.id
    assert second.script_version == candidate.version
    assert second.review_scores is not None
    assert second.review_scores.overall >= 85
    assert len(provider.submissions) == submissions
    assert len(provider.edit_requests) == 1

    session.expire_all()
    assert len(session.scalars(select(ScriptGenerationRun)).all()) == 1
    assert len(session.scalars(select(Script)).all()) == 2
    selected = session.scalars(select(Script).where(Script.selected)).one()
    assert selected.id == candidate.id and selected.status == "approved"
    assert session.get(Project, project.id).status == "script_approved"


@pytest.mark.asyncio
async def test_rejection_feedback_drives_the_second_pass(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    await pipeline.process(project_id=project.id, idempotency_key="run-1")
    run = session.scalars(select(ScriptGenerationRun)).one()
    first_candidate = _pending_candidate(session, run.id)
    _reject(session, first_candidate, "The cold open gives away the finale. Hold it back.")

    result = await pipeline.process(project_id=project.id, idempotency_key="continue-1")
    assert result.status == "script_review_required"
    assert result.script_id is None
    assert result.revision_count == 1

    # Pass 2 edits the rejected version, not the draft, and carries the
    # reviewer's reason to the editor.
    assert len(provider.edit_requests) == 2
    second_request = provider.edit_requests[1]
    assert second_request.script_id == first_candidate.id
    assert second_request.attempt_number == 2
    assert second_request.reviewer_feedback == (
        "The cold open gives away the finale. Hold it back."
    )

    session.expire_all()
    versions = session.scalars(
        select(Script).where(Script.generation_run_id == run.id).order_by(Script.version)
    ).all()
    assert [item.status for item in versions] == ["draft", "rejected", "pending_review"]
    assert [item.editing_pass for item in versions] == [0, 1, 2]
    assert versions[1].rejection_reason == "The cold open gives away the finale. Hold it back."
    assert versions[2].parent_script_id == first_candidate.id
    # The same run was resumed under the new workflow key.
    assert len(session.scalars(select(ScriptGenerationRun)).all()) == 1
    assert session.get(ScriptGenerationRun, run.id).status == "script_review_required"


@pytest.mark.asyncio
async def test_three_rejections_exhaust_the_editing_budget(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    await pipeline.process(project_id=project.id, idempotency_key="run-1")
    run = session.scalars(select(ScriptGenerationRun)).one()
    for attempt in range(1, 4):
        candidate = _pending_candidate(session, run.id)
        assert candidate.editing_pass == attempt
        _reject(session, candidate, f"Still not funny enough ({attempt}).")
        if attempt < 3:
            result = await pipeline.process(
                project_id=project.id, idempotency_key=f"continue-{attempt}"
            )
            assert result.status == "script_review_required"

    submissions = len(provider.submissions)
    with pytest.raises(ScriptEditingExhausted, match="EDITING_PASSES_EXHAUSTED"):
        await pipeline.process(project_id=project.id, idempotency_key="continue-3")
    # The third rejection is final: no fourth editor call, and the run fails
    # rather than waiting on a review it can no longer act on.
    assert len(provider.submissions) == submissions
    assert len(provider.edit_requests) == 3
    assert [item.reviewer_feedback for item in provider.edit_requests] == [
        None,
        "Still not funny enough (1).",
        "Still not funny enough (2).",
    ]
    session.expire_all()
    run = session.get(ScriptGenerationRun, run.id)
    assert run.status == "script_generation_failed"
    assert run.error_code == "EDITING_PASSES_EXHAUSTED"
    assert run.revision_count == 2
    assert session.get(Project, project.id).status == "script_generation_failed"
    failures = session.scalars(select(PipelineFailureEvent)).all()
    assert [item.error_code for item in failures] == ["EDITING_PASSES_EXHAUSTED"]
    assert failures[0].projected_status == "script_generation_failed"
    # The exhausted run is history: the next continuation starts a fresh one
    # rather than resuming a run that can spend nothing.
    fresh = await pipeline.process(project_id=project.id, idempotency_key="continue-4")
    assert fresh.status == "script_review_required"
    assert len(session.scalars(select(ScriptGenerationRun)).all()) == 2


@pytest.mark.asyncio
async def test_the_editing_budget_is_configurable(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider, max_editing_passes=1)
    await pipeline.process(project_id=project.id, idempotency_key="run-1")
    run = session.scalars(select(ScriptGenerationRun)).one()
    assert run.max_editing_passes == 1
    _reject(session, _pending_candidate(session, run.id), "No.")
    with pytest.raises(ScriptEditingExhausted):
        await pipeline.process(project_id=project.id, idempotency_key="continue-1")
    assert len(provider.edit_requests) == 1
    with pytest.raises(ValueError, match="max_editing_passes"):
        ScriptGenerationPipeline(session, blobs, provider, max_editing_passes=0)


@pytest.mark.asyncio
async def test_continuing_without_a_decision_spends_nothing(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    await pipeline.process(project_id=project.id, idempotency_key="run-1")
    submissions = len(provider.submissions)
    result = await pipeline.process(project_id=project.id, idempotency_key="continue-1")
    assert result.status == "script_review_required"
    assert len(provider.submissions) == submissions
    session.expire_all()
    assert len(session.scalars(select(ScriptGenerationRun)).all()) == 1
    assert len(session.scalars(select(Script)).all()) == 2


@pytest.mark.asyncio
async def test_completed_run_is_idempotent(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = FakeScriptGenerationProvider()
    pipeline = ScriptGenerationPipeline(session, blobs, provider)
    first = await pipeline.process(project_id=project.id, idempotency_key="run-1")
    assert first.status == "script_review_required"
    submissions_after_first = len(provider.submissions)
    second = await pipeline.process(project_id=project.id, idempotency_key="run-1")
    assert second.status == first.status
    assert second.generation_run_id == first.generation_run_id
    assert len(provider.submissions) == submissions_after_first

    runs = session.scalars(select(ScriptGenerationRun)).all()
    assert len(runs) == 1
    scripts = session.scalars(select(Script)).all()
    assert len({s.id for s in scripts}) == len(scripts)
    assert len(scripts) == 2


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

    # A fresh idempotency key starts a new lineage against the new analysis;
    # the run paused on the old analysis is not the one it resumes.
    second_result = await pipeline.process(project_id=project.id, idempotency_key="run-2")
    assert second_result.status == "script_review_required"
    session.expire_all()
    runs = session.scalars(
        select(ScriptGenerationRun).order_by(ScriptGenerationRun.created_at)
    ).all()
    assert len(runs) == 2
    assert runs[1].id == second_result.generation_run_id
    assert runs[1].episode_analysis_id == new_record.id
    assert _pending_candidate(session, runs[1].id).episode_analysis_id == new_record.id


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
async def test_the_editors_verdict_is_recorded_but_does_not_loop(tmp_path: Path) -> None:
    """A ``revise`` recommendation is advice for the reviewer, not a reason to spend again."""
    from vidgen.db.script_models import ScriptReview

    session, blobs, project, _record = _database(tmp_path)
    provider = _NeverApprovingProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_review_required"
    session.expire_all()
    run = session.scalars(select(ScriptGenerationRun)).one()
    assert run.error_code is None
    assert run.revision_count == 0
    script_versions = session.scalars(
        select(Script).where(Script.generation_run_id == run.id)
    ).all()
    # draft (v1) plus exactly one edited candidate, waiting for the reviewer
    assert len(script_versions) == 2
    review = session.scalars(select(ScriptReview)).one()
    assert review.approval_recommendation == "revise"
    assert session.get(Project, project.id).status == "script_review_required"


class _UnknownBeatDraftProvider(FakeScriptGenerationProvider):
    """Always writes a draft that references a plot beat outside the plan.

    Regression coverage for the dashboard's Failures/Provider Attempts
    panels: exhausting the draft repair loop on a real (non-provider-outage)
    validation error must still leave a FAILED ProviderAttempt per attempt
    and a PipelineFailureEvent for the run.
    """

    async def write_script(self, request, context):  # type: ignore[override]
        result = await super().write_script(request, context)
        segments = list(result.output.segments)
        segments[0] = segments[0].model_copy(
            update={"plot_beat_ids": [*segments[0].plot_beat_ids, uuid4()]}
        )
        corrupted = result.output.model_copy(update={"segments": segments})
        return result.model_copy(update={"output": corrupted})


@pytest.mark.asyncio
async def test_draft_validation_exhaustion_records_failures(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    provider = _UnknownBeatDraftProvider()
    with pytest.raises(RuntimeError, match="DRAFT_VALIDATION_FAILED"):
        await ScriptGenerationPipeline(session, blobs, provider, max_repair_attempts=1).process(
            project_id=project.id, idempotency_key="run-1"
        )
    session.expire_all()
    run = session.scalars(select(ScriptGenerationRun)).one()
    assert run.error_code == "DRAFT_VALIDATION_FAILED"
    assert session.get(Project, project.id).status == "script_generation_failed"

    attempts = session.scalars(
        select(ProviderAttempt).where(ProviderAttempt.operation == "script.write_script")
    ).all()
    assert len(attempts) == 1
    assert attempts[0].status == "FAILED"
    assert attempts[0].failure_class == "CONTRACT_VALIDATION"
    assert attempts[0].error_code == "UNKNOWN_PLOT_BEAT_REFERENCE"

    failures = session.scalars(select(PipelineFailureEvent)).all()
    assert len(failures) == 1
    assert failures[0].stage == "script_generation"
    assert failures[0].error_code == "UNKNOWN_PLOT_BEAT_REFERENCE"
    assert failures[0].projected_status == "script_generation_failed"


@pytest.mark.asyncio
async def test_a_replacement_run_preserves_the_prior_selected_script(tmp_path: Path) -> None:
    session, blobs, project, _record = _database(tmp_path)
    pipeline = ScriptGenerationPipeline(session, blobs, FakeScriptGenerationProvider())
    await pipeline.process(project_id=project.id, idempotency_key="run-1")
    first_run = session.scalars(select(ScriptGenerationRun)).one()
    approved = _pending_candidate(session, first_run.id)
    _approve(session, approved)

    # A run against changed settings is new material, so it opens a new run
    # rather than resuming the approved one; while its first pass waits for
    # review the approved script stays selected.
    second = await ScriptGenerationPipeline(session, blobs, _NeverApprovingProvider()).process(
        project_id=project.id,
        idempotency_key="run-2",
        setting_overrides={"humor_intensity": 0.9},
    )
    assert second.status == "script_review_required"
    assert second.generation_run_id != first_run.id

    session.expire_all()
    selected = session.scalars(
        select(Script).where(Script.project_id == project.id, Script.selected)
    ).all()
    assert len(selected) == 1
    assert selected[0].id == approved.id


@pytest.mark.asyncio
async def test_revised_script_and_its_review_commit_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for a Copilot review finding: _persist_script_version() used
    # to commit its own transaction, so a crash between persisting a revised
    # script version and persisting its ScriptReview/ScriptEditRecord rows
    # left an orphaned child script with no review row. On resume, restart
    # detection (_existing_review) can't find it, so the pipeline calls the
    # provider again and creates a second child of the same parent -
    # duplicate revision branches and an ambiguous "next version". Simulate
    # the crash by failing right after the script version is persisted but
    # before the review row is built, and confirm nothing is left committed.
    from vidgen.db.script_repository import ScriptRepository

    session, blobs, project, _record = _database(tmp_path)
    provider = _NeverApprovingProvider()

    original = ScriptRepository.next_review_sequence
    calls = {"count": 0}

    def _fail_once(self, script_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash before review row is persisted")
        return original(self, script_id)

    monkeypatch.setattr(ScriptRepository, "next_review_sequence", _fail_once)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await ScriptGenerationPipeline(session, blobs, provider).process(
            project_id=project.id, idempotency_key="run-1"
        )
    session.rollback()

    draft = session.scalars(select(Script).where(Script.parent_script_id.is_(None))).one()
    orphans = session.scalars(select(Script).where(Script.parent_script_id == draft.id)).all()
    assert orphans == []

    monkeypatch.setattr(ScriptRepository, "next_review_sequence", original)
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_review_required"


class _EmptyBeatDraftProvider(FakeScriptGenerationProvider):
    """Writes a sound draft and then appends two empty beats to it.

    One is the shape a model actually emits - a PAUSE with no text, the only
    type the contract lets be empty - and one is a narration segment whose text
    is blank (built with ``model_copy`` to bypass the contract, as a raw model
    response would). Both must be cleared before the script is persisted.
    """

    async def write_script(self, request, context):  # type: ignore[override]
        result = await super().write_script(request, context)
        segments = list(result.output.segments)
        last = segments[-1]
        pause = last.model_copy(
            update={
                "segment_id": uuid4(),
                "sequence": last.sequence + 1,
                "type": "PAUSE",
                "text": "",
                "joke_annotations": [],
                "visual_gag": None,
                "content_hash": "0" * 64,
            }
        )
        blank = last.model_copy(
            update={
                "segment_id": uuid4(),
                "sequence": last.sequence + 2,
                "text": "   ",
                "joke_annotations": [],
                "visual_gag": None,
                "content_hash": "1" * 64,
            }
        )
        padded = result.output.model_copy(update={"segments": [*segments, pause, blank]})
        return result.model_copy(update={"output": padded})


@pytest.mark.asyncio
async def test_empty_beats_are_cleared_before_the_script_is_persisted(tmp_path: Path) -> None:
    from services.script.canonicalize import EMPTY_SEGMENT_DROPPED
    from vidgen.db.models import Asset
    from vidgen.db.narration_repository import NarrationRepository

    session, blobs, project, _record = _database(tmp_path)
    provider = _EmptyBeatDraftProvider()
    result = await ScriptGenerationPipeline(session, blobs, provider).process(
        project_id=project.id, idempotency_key="run-1"
    )
    assert result.status == "script_review_required"
    script_record = _pending_candidate(session, result.generation_run_id)
    _approve(session, script_record)

    session.expire_all()
    script_record = session.get(Script, script_record.id)
    assert script_record is not None
    rows = session.scalars(
        select(ScriptSegment)
        .where(ScriptSegment.script_id == script_record.id)
        .order_by(ScriptSegment.sequence)
    ).all()
    assert rows and all(row.text.strip() for row in rows)
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert all(row.segment_type == "NARRATION" for row in rows)
    assert script_record.actual_word_count == sum(len(row.text.split()) for row in rows)

    asset = session.get(Asset, script_record.canonical_script_asset_id)
    assert asset is not None
    persisted = RecapScript.model_validate_json(blobs.read(asset.storage_key))
    dropped = [note for note in persisted.warnings if note.code == EMPTY_SEGMENT_DROPPED]
    assert len(dropped) == 2
    assert {note.message.split(" ")[0] for note in dropped} == {"PAUSE", "NARRATION"}

    # What narration reads is exactly the cleared script.
    _script, segments = NarrationRepository(session).authoritative_script(project.id)
    assert [segment.id for segment in segments] == [row.id for row in rows]
