"""The local fake voice-profile bootstrap used by the README setup path."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import scripts.create_local_voice_profile as cli
from services.narration.local_voice_profile import (
    FAKE_PROVIDER_VOICE_ID,
    VOICE_PROFILE_SETTING,
    LocalVoiceProfileError,
    ensure_local_voice_profile,
    local_voice_profile_id,
)
from vidgen.db.base import Base
from vidgen.db.models import Project
from vidgen.db.narration_models import VoiceProfileRecord


@pytest.fixture
def session(tmp_path: Path) -> Generator[Session, None, None]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'voice.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as opened:
        yield opened


def make_project(session: Session, **settings: object) -> Project:
    project = Project(
        name="local",
        owner_subject="local-user",
        status="awaiting_upload",
        target_duration_seconds=300,
        visual_style="flat editorial cartoon",
        humor_intensity=5,
        settings=dict(settings),
    )
    session.add(project)
    session.commit()
    return project


def test_creates_a_deterministic_project_scoped_fake_profile(session: Session) -> None:
    project = make_project(session)
    result = ensure_local_voice_profile(session, project_id=project.id)
    assert result.action == "created"
    assert result.provider == "fake"
    assert result.voice_profile_id == local_voice_profile_id(project.id)
    profile = session.get(VoiceProfileRecord, result.voice_profile_id)
    assert profile is not None
    assert profile.project_id == project.id
    assert profile.provider_voice_id == FAKE_PROVIDER_VOICE_ID
    assert profile.version == 1
    assert project.settings[VOICE_PROFILE_SETTING] == str(result.voice_profile_id)


def test_is_idempotent_and_creates_no_second_profile(session: Session) -> None:
    project = make_project(session)
    first = ensure_local_voice_profile(session, project_id=project.id)
    second = ensure_local_voice_profile(session, project_id=project.id)
    assert second.voice_profile_id == first.voice_profile_id
    assert second.action == "unchanged"
    assert session.query(VoiceProfileRecord).count() == 1


def test_reuses_an_unselected_local_profile_instead_of_creating_another(
    session: Session,
) -> None:
    project = make_project(session)
    created = ensure_local_voice_profile(session, project_id=project.id)
    project.settings = {}
    session.commit()
    again = ensure_local_voice_profile(session, project_id=project.id)
    assert again.action == "assigned"
    assert again.voice_profile_id == created.voice_profile_id
    assert session.query(VoiceProfileRecord).count() == 1


def test_never_overwrites_an_explicitly_selected_production_profile(session: Session) -> None:
    project = make_project(session)
    production = VoiceProfileRecord(
        project_id=project.id,
        provider="elevenlabs",
        provider_voice_id="production-voice",
        model="eleven_v3",
        language="en",
        version=2,
        configuration={"default_pace": 1.0},
        configuration_hash="a" * 64,
    )
    session.add(production)
    session.flush()
    project.settings = {VOICE_PROFILE_SETTING: str(production.id)}
    session.commit()

    result = ensure_local_voice_profile(session, project_id=project.id)
    assert result.action == "unchanged"
    assert result.voice_profile_id == production.id
    assert result.provider == "elevenlabs"
    assert session.query(VoiceProfileRecord).count() == 1


def test_repairs_a_selection_that_does_not_resolve(session: Session) -> None:
    project = make_project(session, **{VOICE_PROFILE_SETTING: str(uuid4())})
    result = ensure_local_voice_profile(session, project_id=project.id)
    assert result.action == "repaired"
    assert result.voice_profile_id == local_voice_profile_id(project.id)
    assert project.settings[VOICE_PROFILE_SETTING] == str(result.voice_profile_id)


def test_repairs_a_cross_project_selection(session: Session) -> None:
    other = make_project(session)
    foreign = VoiceProfileRecord(
        project_id=other.id,
        provider="fake",
        provider_voice_id="other",
        model="fake-tts",
        language="en",
        version=1,
        configuration={},
        configuration_hash="b" * 64,
    )
    session.add(foreign)
    session.commit()
    project = make_project(session, **{VOICE_PROFILE_SETTING: str(foreign.id)})
    result = ensure_local_voice_profile(session, project_id=project.id)
    assert result.action == "repaired"
    assert result.voice_profile_id != foreign.id


def test_missing_project_is_rejected(session: Session) -> None:
    with pytest.raises(LocalVoiceProfileError, match="does not exist"):
        ensure_local_voice_profile(session, project_id=uuid4())


def test_production_provider_is_refused(session: Session) -> None:
    project = make_project(session)
    with pytest.raises(LocalVoiceProfileError, match="only creates fake"):
        ensure_local_voice_profile(session, project_id=project.id, provider="elevenlabs")


def test_cli_prints_the_voice_profile_id(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(session)
    bind = session.get_bind()
    monkeypatch.setattr(cli, "build_engine", lambda: bind)
    monkeypatch.setattr(cli, "session_factory", lambda engine: sessionmaker(bind=engine))

    assert cli.main([str(project.id), "--provider", "fake"]) == 0
    printed = capsys.readouterr().out
    assert f"voice_profile_id={local_voice_profile_id(project.id)}" in printed
    assert "action=created" in printed

    assert cli.main([str(project.id)]) == 0
    assert "action=unchanged" in capsys.readouterr().out


def test_cli_reports_a_missing_project_without_traceback(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bind = session.get_bind()
    monkeypatch.setattr(cli, "build_engine", lambda: bind)
    monkeypatch.setattr(cli, "session_factory", lambda engine: sessionmaker(bind=engine))
    assert cli.main([str(uuid4())]) == 2
    assert "does not exist" in capsys.readouterr().out
