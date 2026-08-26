"""Start or resume T10 for the selected project evidence package."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.settings import get_settings
from services.analysis.fake_provider import FakeEpisodeAnalysisProvider
from services.analysis.openai_adapter import OpenAIAnalysisConfig, OpenAIEpisodeAnalysisProvider
from services.analysis.pipeline import EpisodeAnalysisPipeline
from services.analysis.provider import EpisodeAnalysisProvider
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord
from vidgen.db.models import Project
from vidgen.db.session import build_engine
from vidgen.db.workflow_models import EvidencePackageRecord
from vidgen.storage.blob import FilesystemBlobStore


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    settings = get_settings()
    with Session(build_engine(settings.database_url), expire_on_commit=False) as session:
        evidence = session.scalar(
            select(EvidencePackageRecord).where(
                EvidencePackageRecord.project_id == args.project_id, EvidencePackageRecord.selected
            )
        )
        if evidence is None:
            parser.error("project has no selected T09 evidence package")
        if args.provider == "fake":
            provider: EpisodeAnalysisProvider = FakeEpisodeAnalysisProvider()
        else:
            key = os.getenv("OPENAI_API_KEY") or settings.openai_api_key
            if not key:
                parser.error("OPENAI_API_KEY is required for --provider openai")
            provider = OpenAIEpisodeAnalysisProvider(
                OpenAIAnalysisConfig(api_key=key, model=settings.analysis_model)
            )
        result = await EpisodeAnalysisPipeline(
            session,
            FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode()),
            provider,
        ).process(
            project_id=args.project_id,
            evidence_package_id=evidence.id,
            idempotency_key=args.idempotency_key or f"episode-analysis:{uuid4()}",
        )
        record = session.get(EpisodeAnalysisRecord, result.episode_analysis_id)
        project = session.get(Project, args.project_id)
        if record is None or project is None:
            raise RuntimeError("analysis persistence failed")
        print(
            f"analysis_run_id={result.analysis_run_id} "
            f"episode_analysis_id={result.episode_analysis_id} version={result.version} "
            f"characters={record.character_count} locations={record.location_count} "
            f"scenes={record.scene_count} plot_beats={record.plot_beat_count} "
            f"validation={result.validation_report.valid} project_status={project.status}"
        )


if __name__ == "__main__":
    asyncio.run(main())
