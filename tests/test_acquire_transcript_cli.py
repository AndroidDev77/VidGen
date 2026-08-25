from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.settings import APISettings
from scripts.acquire_transcript import (
    _latest_audio_for_source,
    _subtitle_languages,
    build_parser,
)
from vidgen.db.base import Base
from vidgen.db.models import AudioAsset, Project
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import FilesystemBlobStore


def test_cli_accepts_repeated_sidecar_asset_ids() -> None:
    project_id = UUID("00000000-0000-0000-0000-000000000001")
    first = UUID("00000000-0000-0000-0000-000000000002")
    second = UUID("00000000-0000-0000-0000-000000000003")
    args = build_parser().parse_args(
        [
            str(project_id),
            "--sidecar-asset-id",
            str(first),
            "--sidecar-asset-id",
            str(second),
        ]
    )
    assert args.project_id == project_id
    assert args.sidecar_asset_id == [first, second]


def test_language_option_overrides_configured_subtitle_languages() -> None:
    settings = APISettings(subtitle_languages=("en",))
    assert _subtitle_languages("ES", settings) == ("es",)
    assert _subtitle_languages(None, settings) == ("en",)


def test_latest_audio_is_selected_from_source_provenance(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(name="cli provenance", visual_style="flat")
        session.add(project)
        session.flush()
        assets = AssetService(session, FilesystemBlobStore(tmp_path / "blobs", b"secret"))
        selected_source = assets.store(
            content=b"selected",
            kind="source_video",
            media_type="video/mp4",
            project_id=project.id,
            idempotency_key="selected-source",
        )
        other_source = assets.store(
            content=b"other",
            kind="source_video",
            media_type="video/mp4",
            project_id=project.id,
            idempotency_key="other-source",
        )
        correct_asset = assets.store(
            content=b"correct audio",
            kind="transcription_audio",
            media_type="audio/wav",
            project_id=project.id,
            parent_asset_ids=(selected_source.id,),
            idempotency_key="correct-audio",
        )
        wrong_asset = assets.store(
            content=b"wrong audio",
            kind="transcription_audio",
            media_type="audio/wav",
            project_id=project.id,
            parent_asset_ids=(other_source.id,),
            idempotency_key="wrong-audio",
        )
        now = datetime.now(UTC)
        correct = AudioAsset(
            project_id=project.id,
            asset_id=correct_asset.id,
            kind="transcription_audio",
            duration_seconds=1,
            created_at=now,
            updated_at=now,
        )
        wrong = AudioAsset(
            project_id=project.id,
            asset_id=wrong_asset.id,
            kind="transcription_audio",
            duration_seconds=1,
            created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
        )
        session.add_all([correct, wrong])
        session.commit()

        selected = _latest_audio_for_source(
            session,
            project_id=project.id,
            source_asset_id=selected_source.id,
        )
        assert selected is not None and selected.id == correct.id
