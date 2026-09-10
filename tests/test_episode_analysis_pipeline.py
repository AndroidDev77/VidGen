from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.instrumentation import REDUCE_OPERATION, SCENE_OPERATION
from services.analysis.pipeline import EpisodeAnalysisPipeline, _excerpts
from vidgen.db.base import Base
from vidgen.db.cost_models import (
    CostLedgerEntry,
    PricingVersion,
    ProjectBudget,
    ProviderAttempt,
    ProviderPriceRate,
)
from vidgen.db.cost_repository import BudgetExceededError
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


def test_long_transcript_evidence_is_split_into_bounded_excerpts() -> None:
    row = SceneEvidenceRecord(
        evidence_package_id=uuid4(),
        scene_sequence=0,
        source_start_seconds=0,
        source_end_seconds=1,
        frame_asset_ids=[str(uuid4())],
        evidence={
            "transcript_items": [
                {
                    "text": "x" * 4001,
                    "speaker_label": "speaker_001",
                    "source_asset_id": str(uuid4()),
                    "source_range": {"start_seconds": 0, "end_seconds": 1},
                }
            ]
        },
    )
    excerpts = _excerpts(row)
    assert [len(item.text) for item in excerpts] == [4000, 1]


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
    assert len(resumed_provider.submissions) == 1
    assert resumed_provider.submissions[0].startswith("episode-reduce:")


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
    assert len([key for key in resumed.submissions if key.startswith("episode-scene:")]) == 1


# -- T23 cost recording -------------------------------------------------------


class _MeteredProvider(FakeEpisodeAnalysisProvider):
    """Fake provider that reports token usage like the OpenAI Responses API does."""

    #: A name the published rate table resolves, so the fallback path is real.
    model = "gpt-5.6"
    #: Cached share of ``input_tokens``, as OpenAI reports it.
    cached_input_tokens: int | None = None

    def _metadata(self, request, context):  # type: ignore[no-untyped-def]
        metadata = super()._metadata(request, context)
        return metadata.model_copy(
            update={
                "input_tokens": 1000,
                "cached_input_tokens": self.cached_input_tokens,
                "output_tokens": 500,
            }
        )


def _price_episode_analysis(
    session: Session, project_id, *, hard_cap: Decimal = Decimal("10")
) -> None:
    now = datetime.now(UTC)
    version = PricingVersion(
        name=f"episode-analysis-test-{project_id}",
        currency="USD",
        source_metadata={"source": "fixture"},
        verification_date=now.date(),
        activated_at=now,
    )
    session.add(version)
    session.flush()
    for operation in (SCENE_OPERATION, REDUCE_OPERATION):
        for unit in ("INPUT_TOKEN", "OUTPUT_TOKEN"):
            session.add(
                ProviderPriceRate(
                    pricing_version_id=version.id,
                    provider=_MeteredProvider.provider,
                    model=_MeteredProvider.model,
                    operation=operation,
                    usage_unit=unit,
                    unit_size=Decimal("1000"),
                    unit_price=Decimal("0.010000"),
                    effective_start=now,
                    active=True,
                    source_reference="fixture://pricing",
                )
            )
    session.add(
        ProjectBudget(
            project_id=project_id,
            warning_cap=hard_cap / 2,
            hard_cap=hard_cap,
            currency="USD",
            policy_version="v1",
        )
    )
    session.commit()


@pytest.mark.asyncio
async def test_every_analysis_call_reaches_the_cost_ledger(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    _price_episode_analysis(session, project.id)
    await EpisodeAnalysisPipeline(session, blobs, _MeteredProvider()).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="priced"
    )
    attempts = list(session.scalars(select(ProviderAttempt)))
    operations = sorted(attempt.operation for attempt in attempts)
    # Two scenes are mapped, then reduced once.
    assert operations == [REDUCE_OPERATION, SCENE_OPERATION, SCENE_OPERATION]
    assert all(attempt.status == "SUCCEEDED" for attempt in attempts)
    assert all(attempt.provider_request_id for attempt in attempts)
    assert all(
        attempt.usage
        == [
            {"unit": "INPUT_TOKEN", "quantity": 1000},
            {"unit": "OUTPUT_TOKEN", "quantity": 500},
        ]
        for attempt in attempts
    )
    entries = list(session.scalars(select(CostLedgerEntry)))
    # 1000 input + 500 output tokens at $0.01 / 1000 tokens is $0.015 a call.
    assert [entry.actual_amount for entry in entries] == [Decimal("0.015000")] * 3
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project.id))
    assert budget is not None
    assert budget.committed_amount == Decimal("0.045000")
    assert budget.reserved_amount == Decimal("0")


@pytest.mark.asyncio
async def test_failed_reduce_releases_its_reservation(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    _price_episode_analysis(session, project.id)

    class FailingReduce(_MeteredProvider):
        async def synthesize_episode(self, request, context):  # type: ignore[no-untyped-def]
            raise TimeoutError("interrupted")

    with pytest.raises(TimeoutError):
        await EpisodeAnalysisPipeline(session, blobs, FailingReduce()).process(
            project_id=project.id, evidence_package_id=evidence.id, idempotency_key="failing"
        )
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project.id))
    assert budget is not None
    # The scene calls committed; the failed reduce attempts hold no budget.
    assert budget.reserved_amount == Decimal("0")
    assert budget.committed_amount == Decimal("0.030000")
    reduce_attempts = [
        row for row in session.scalars(select(ProviderAttempt)) if row.operation == REDUCE_OPERATION
    ]
    assert reduce_attempts and all(row.status == "FAILED" for row in reduce_attempts)


@pytest.mark.asyncio
async def test_analysis_is_denied_when_the_budget_cannot_cover_it(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    _price_episode_analysis(session, project.id, hard_cap=Decimal("0.000001"))
    with pytest.raises(BudgetExceededError):
        await EpisodeAnalysisPipeline(session, blobs, _MeteredProvider()).process(
            project_id=project.id, evidence_package_id=evidence.id, idempotency_key="denied"
        )
    assert not list(session.scalars(select(CostLedgerEntry)))


@pytest.mark.asyncio
async def test_a_retry_after_a_provider_error_commits_the_successful_call(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    _price_episode_analysis(session, project.id)

    class FlakyReduce(_MeteredProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reduce_calls = 0

        async def synthesize_episode(self, request, context):  # type: ignore[no-untyped-def]
            self.reduce_calls += 1
            if self.reduce_calls == 1:
                raise TimeoutError("transient")
            return await super().synthesize_episode(request, context)

    provider = FlakyReduce()
    result = await EpisodeAnalysisPipeline(session, blobs, provider).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="flaky"
    )
    assert result.validation_report.valid and provider.reduce_calls == 2
    budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project.id))
    assert budget is not None
    # Two scenes plus the reduce retry are billed; the failed first reduce is not.
    assert budget.committed_amount == Decimal("0.045000")
    assert budget.reserved_amount == Decimal("0")


@pytest.mark.asyncio
async def test_an_uncatalogued_model_is_billed_at_the_fallback_rate(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    # A budget, but no pricing catalog at all: the deployment's analysis model
    # has no rate, which is the state every real deployment starts in.
    session.add(
        ProjectBudget(
            project_id=project.id,
            warning_cap=Decimal("5"),
            hard_cap=Decimal("10"),
            currency="USD",
            policy_version="v1",
        )
    )
    session.commit()
    await EpisodeAnalysisPipeline(session, blobs, _MeteredProvider()).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="uncatalogued"
    )
    entries = list(session.scalars(select(CostLedgerEntry)))
    # gpt-5.6 resolves to the Terra tier: 1000 input at $2/M is $0.002, and
    # 500 output at $12/M is $0.006.
    assert [entry.actual_amount for entry in entries] == [Decimal("0.008000")] * 3
    assert all(entry.pricing_version_id is None for entry in entries)
    attempts = list(session.scalars(select(ProviderAttempt)))
    assert all(row.redacted_metadata["pricing_status"] == "fallback" for row in attempts)


@pytest.mark.asyncio
async def test_cached_input_tokens_are_billed_at_the_cached_rate(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    session.add(
        ProjectBudget(
            project_id=project.id,
            warning_cap=Decimal("5"),
            hard_cap=Decimal("10"),
            currency="USD",
            policy_version="v1",
        )
    )
    session.commit()
    provider = _MeteredProvider()
    provider.cached_input_tokens = 400
    await EpisodeAnalysisPipeline(session, blobs, provider).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="cached"
    )
    entries = list(session.scalars(select(CostLedgerEntry)))
    # 600 uncached input at $2/M, 400 cached at $0.20/M, 500 output at $12/M.
    assert [entry.actual_amount for entry in entries] == [Decimal("0.007280")] * 3
    # The units stay disjoint, so the recorded usage sums to the 1500 tokens
    # the provider reported rather than double-counting the cache hits.
    assert entries[0].usage == [
        {"unit": "CACHED_INPUT_TOKEN", "quantity": 400},
        {"unit": "INPUT_TOKEN", "quantity": 600},
        {"unit": "OUTPUT_TOKEN", "quantity": 500},
    ]


@pytest.mark.asyncio
async def test_a_model_with_no_published_price_records_its_tokens_at_zero(
    tmp_path: Path,
) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    session.add(
        ProjectBudget(
            project_id=project.id,
            warning_cap=Decimal("5"),
            hard_cap=Decimal("10"),
            currency="USD",
            policy_version="v1",
        )
    )
    session.commit()

    class UnknownModel(_MeteredProvider):
        model = "some-unreleased-model"

    await EpisodeAnalysisPipeline(session, blobs, UnknownModel()).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="unknown"
    )
    entries = list(session.scalars(select(CostLedgerEntry)))
    # Nothing is invented for a model nobody has published a price for.
    assert [entry.actual_amount for entry in entries] == [Decimal("0")] * 3
    attempts = list(session.scalars(select(ProviderAttempt)))
    assert all(row.redacted_metadata["pricing_status"] == "unpriced" for row in attempts)
    # The tokens are still recorded, so the spend can be priced retroactively.
    assert all(row.usage for row in attempts)


@pytest.mark.asyncio
async def test_a_catalogued_model_never_uses_the_fallback(tmp_path: Path) -> None:
    session, blobs, project, evidence = _database(tmp_path)
    _price_episode_analysis(session, project.id)
    await EpisodeAnalysisPipeline(session, blobs, _MeteredProvider()).process(
        project_id=project.id, evidence_package_id=evidence.id, idempotency_key="catalogued"
    )
    attempts = list(session.scalars(select(ProviderAttempt)))
    assert attempts and all(
        row.redacted_metadata["pricing_status"] == "catalog" for row in attempts
    )
    assert all(row.pricing_version_id is not None for row in attempts)
