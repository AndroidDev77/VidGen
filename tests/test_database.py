from __future__ import annotations

from sqlalchemy import create_engine, inspect

import vidgen.db.models
import vidgen.db.transcription_models
import vidgen.db.upload_models  # noqa: F401
from vidgen.db.base import Base


def test_core_schema_creates_with_expected_tables_and_indexes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    expected = {
        "projects",
        "source_videos",
        "characters",
        "locations",
        "scenes",
        "plot_beats",
        "scripts",
        "script_segments",
        "shots",
        "generated_images",
        "generated_videos",
        "audio_assets",
        "qa_results",
        "render_jobs",
        "transcription_runs",
        "transcription_chunks",
        "transcripts",
        "transcript_segments",
        "speaker_turns",
        "assets",
        "asset_dependencies",
        "upload_sessions",
        "upload_parts",
    }
    assert expected <= set(inspector.get_table_names())
    indexes = {index["name"] for index in inspector.get_indexes("shots")}
    assert "ix_shots_selected_image" in indexes
    assert "ix_shots_selected_video" in indexes
