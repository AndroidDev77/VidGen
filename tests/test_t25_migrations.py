"""T25 migration, database constraints and schema-drift checks.

The migration must leave exactly one Alembic head, upgrade from empty, downgrade
cleanly while nothing has been published, refuse to destroy publication
provenance once something has, and re-upgrade. The constraints are asserted
directly, because they are the last line of defence against a duplicate video.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db
from scripts.export_schemas import rendered_schemas
from tests.publication_fixtures import build_publishable_project, connect_fake_channel
from vidgen.contracts.publication import (
    ALLOWED_TRANSITIONS,
    PublicationAssetKind,
    PublicationAssetStatus,
    PublicationStatus,
)
from vidgen.db.publication_models import (
    PublicationAsset,
    PublicationRun,
    YouTubeUploadSession,
    transition_check_expression,
)
from vidgen.storage.blob import FilesystemBlobStore

ROOT = Path(__file__).resolve().parents[1]
T25_TABLES = {
    "youtube_connections",
    "youtube_connection_secrets",
    "youtube_oauth_states",
    "publication_runs",
    "youtube_upload_sessions",
    "publication_assets",
}
PREVIOUS_HEAD = "0018_final_editorial_qa"


def config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_t25_is_the_only_head_and_follows_t22() -> None:
    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["0019_youtube_publication"]
    assert script.get_revision("0019_youtube_publication").down_revision == PREVIOUS_HEAD


def test_upgrade_downgrade_upgrade_is_clean_with_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'publication.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    assert T25_TABLES <= set(inspect(engine).get_table_names())
    command.check(config())
    command.downgrade(config(), PREVIOUS_HEAD)
    assert not T25_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    assert T25_TABLES <= set(inspect(engine).get_table_names())
    command.check(config())


def test_the_migration_is_additive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'additive.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), PREVIOUS_HEAD)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config(), "head")
    after = set(inspect(engine).get_table_names())
    assert before <= after, "T25 removes nothing that T01-T24 created"
    assert after - before == T25_TABLES


def test_the_downgrade_refuses_to_destroy_publication_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'provenance.db'}"
    monkeypatch.setenv("VIDGEN_DATABASE_URL", url)
    engine = create_engine(url)
    command.upgrade(config(), "head")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FilesystemBlobStore(tmp_path / "blobs", b"test-secret")
    with factory() as session:
        build_publishable_project(session, store)
        connect_fake_channel(session)
    with pytest.raises(RuntimeError, match="publication provenance"):
        command.downgrade(config(), PREVIOUS_HEAD)
    assert T25_TABLES <= set(inspect(engine).get_table_names())


# -- constraints ---------------------------------------------------------------
@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'constraints.db'}")
    vidgen.db.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs", b"test-secret")


def _run(session: Session, store: FilesystemBlobStore, **overrides: object) -> PublicationRun:
    fixture = build_publishable_project(session, store)
    connection, _, _ = connect_fake_channel(session)
    payload: dict[str, object] = {
        "project_id": fixture.project_id,
        "final_render_asset_id": fixture.final_asset_id,
        "render_job_id": fixture.render_job_id,
        "final_editorial_run_id": fixture.final_editorial_run_id,
        "completion_gate_id": fixture.completion_gate_id,
        "approval_id": fixture.approval_id,
        "connection_id": connection.id,
        "channel_id": connection.channel_id,
        "owner_subject": fixture.owner_subject,
        "publication_identity": "a" * 64,
        "idempotency_key": "publish:1",
        "metadata_hash": "b" * 64,
        "input_hash": "c" * 64,
        "render_identity": "d" * 64,
        "status": PublicationStatus.DRAFT.value,
        "current_phase": "ELIGIBILITY",
        "draft_metadata": {},
    }
    payload.update(overrides)
    run = PublicationRun(**payload)
    session.add(run)
    return run


def test_the_transition_constraint_is_generated_from_the_application_table() -> None:
    """The database and the application can never disagree about the machine."""
    expression = transition_check_expression()
    for source, targets in ALLOWED_TRANSITIONS.items():
        if not targets:
            continue
        assert f"previous_status = '{source.value}'" in expression
        for target in targets:
            assert f"'{target.value}'" in expression
    # A forbidden pair is genuinely absent.
    assert (
        "previous_status = 'PRIVATE_READY' AND status IN ('FAILED','HUMAN_REVIEW_REQUIRED',"
        "'PUBLISHED','QUOTA_BLOCKED','REAUTHORIZATION_REQUIRED','UPLOADING_CAPTIONS',"
        "'UPLOADING_THUMBNAIL','VISIBILITY_UPDATING')" in expression
    )


def test_the_database_refuses_an_illegal_transition(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(
            session,
            store,
            previous_status=PublicationStatus.PRIVATE_READY.value,
            status=PublicationStatus.UPLOADING.value,
            video_id="vid1",
            processing_state="succeeded",
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_state_after_upload_must_name_its_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(
            session,
            store,
            previous_status=PublicationStatus.UPLOADING.value,
            status=PublicationStatus.PROCESSING.value,
            video_id=None,
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_published_video_must_have_finished_processing(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(
            session,
            store,
            previous_status=PublicationStatus.VISIBILITY_UPDATING.value,
            status=PublicationStatus.PUBLISHED.value,
            video_id="vid1",
            processing_state="processing",
            actual_privacy="private",
            visibility_decision_at=datetime.now(UTC),
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_non_private_state_requires_an_explicit_visibility_decision(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(
            session,
            store,
            previous_status=PublicationStatus.VISIBILITY_UPDATING.value,
            status=PublicationStatus.PUBLISHED.value,
            video_id="vid1",
            processing_state="succeeded",
            actual_privacy="public",
            visibility_decision_at=None,
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_publication_per_stable_identity(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(session, store)
        session.commit()
        _run(session, store, idempotency_key="publish:2")
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_youtube_video_id_belongs_to_exactly_one_publication(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        _run(
            session,
            store,
            previous_status=PublicationStatus.UPLOADING.value,
            status=PublicationStatus.PROCESSING.value,
            video_id="shared-video",
        )
        session.commit()
        _run(
            session,
            store,
            publication_identity="e" * 64,
            idempotency_key="publish:2",
            previous_status=PublicationStatus.UPLOADING.value,
            status=PublicationStatus.PROCESSING.value,
            video_id="shared-video",
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_upload_offset_can_never_exceed_the_total(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        run = _run(session, store)
        session.commit()
        session.add(
            YouTubeUploadSession(
                publication_run_id=run.id,
                session_uri_ciphertext=b"x",
                session_uri_nonce=b"0" * 12,
                session_uri_hash="f" * 64,
                encryption_key_version="v1",
                total_bytes=100,
                confirmed_offset=101,
                chunk_bytes=262144,
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_only_one_active_session_per_publication(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        run = _run(session, store)
        session.commit()
        for suffix in ("a", "b"):
            session.add(
                YouTubeUploadSession(
                    publication_run_id=run.id,
                    session_uri_ciphertext=b"x",
                    session_uri_nonce=b"0" * 12,
                    session_uri_hash=suffix * 64,
                    encryption_key_version="v1",
                    total_bytes=100,
                    confirmed_offset=0,
                    chunk_bytes=262144,
                    status="active",
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_completed_session_confirmed_every_byte_and_names_its_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        run = _run(session, store)
        session.commit()
        session.add(
            YouTubeUploadSession(
                publication_run_id=run.id,
                session_uri_ciphertext=b"x",
                session_uri_nonce=b"0" * 12,
                session_uri_hash="a" * 64,
                encryption_key_version="v1",
                total_bytes=100,
                confirmed_offset=50,
                chunk_bytes=262144,
                status="completed",
                video_id="v",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_successful_caption_per_language_and_name(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        run = _run(session, store)
        session.commit()
        for suffix in ("a", "b"):
            session.add(
                PublicationAsset(
                    publication_run_id=run.id,
                    kind=PublicationAssetKind.CAPTION.value,
                    status=PublicationAssetStatus.SUCCEEDED.value,
                    provider_resource_id=f"cap-{suffix}",
                    language="en",
                    name="VidGen recap",
                    completed_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_video_and_one_thumbnail_row_per_publication(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        run = _run(session, store)
        session.commit()
        for _ in range(2):
            session.add(
                PublicationAsset(
                    publication_run_id=run.id,
                    kind=PublicationAssetKind.THUMBNAIL.value,
                    status=PublicationAssetStatus.PENDING.value,
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_connected_channel_must_carry_a_credential(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    from vidgen.db.publication_models import YouTubeConnection

    with factory() as session:
        session.add(
            YouTubeConnection(
                owner_subject="local-user",
                channel_id="UCx",
                channel_title="x",
                granted_scopes=[],
                status="connected",
                encryption_key_version="",
                credential_present=False,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_oauth_state_must_expire_after_it_was_created(
    factory: sessionmaker[Session],
) -> None:
    from vidgen.db.publication_models import YouTubeOAuthState

    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            YouTubeOAuthState(
                state_hash="a" * 64,
                owner_subject="local-user",
                code_verifier_ciphertext=b"x",
                code_verifier_nonce=b"0" * 12,
                encryption_key_version="v1",
                redirect_uri="http://localhost/cb",
                requested_scopes=[],
                expires_at=now - timedelta(minutes=1),
                created_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_state_hash_is_unique(factory: sessionmaker[Session]) -> None:
    from vidgen.db.publication_models import YouTubeOAuthState

    now = datetime.now(UTC)
    with factory() as session:
        for _ in range(2):
            session.add(
                YouTubeOAuthState(
                    id=uuid4(),
                    state_hash="a" * 64,
                    owner_subject="local-user",
                    code_verifier_ciphertext=b"x",
                    code_verifier_nonce=b"0" * 12,
                    encryption_key_version="v1",
                    redirect_uri="http://localhost/cb",
                    requested_scopes=[],
                    expires_at=now + timedelta(minutes=10),
                    created_at=now,
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


# -- schema drift --------------------------------------------------------------
def test_the_exported_json_schemas_are_current() -> None:
    stale = [
        path.name
        for path, content in rendered_schemas().items()
        if ("Publication" in path.name or "YouTube" in path.name or "OAuth" in path.name)
        and (not path.exists() or path.read_text() != content)
    ]
    assert not stale, f"stale T25 contract schemas: {stale}"


def test_every_public_t25_contract_has_an_exported_schema() -> None:
    exported = {path.name for path in rendered_schemas()}
    for name in (
        "YouTubeConnection",
        "YouTubeOAuthState",
        "YouTubeChannel",
        "PublicationDraft",
        "PublicationMetadata",
        "PublicationProviderRequest",
        "PublicationProviderResult",
        "ResumableUploadCheckpoint",
        "PublicationAssetResult",
        "PublicationAttempt",
        "PublicationProgress",
        "PublicationResult",
        "PublicationGate",
        "PublicationFailure",
    ):
        assert f"{name}.v1.json" in exported, name


def test_no_publication_contract_can_carry_a_credential() -> None:
    """A field-name audit over every exported T25 schema."""
    forbidden = (
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization_code",
        "session_uri",
        "code_verifier",
        "upload_uri",
    )
    for path, content in rendered_schemas().items():
        if not any(part in path.name for part in ("Publication", "YouTube", "OAuth")):
            continue
        lowered = content.lower()
        for name in forbidden:
            # ``session_uri_hash`` is evidence, not a credential, and is allowed.
            occurrences = lowered.count(f'"{name}"')
            assert occurrences == 0, f"{path.name} exposes {name}"


def test_every_contract_forbids_unknown_fields() -> None:
    from vidgen.contracts import publication

    for name in dir(publication):
        candidate = getattr(publication, name)
        if not isinstance(candidate, type):
            continue
        config = getattr(candidate, "model_config", None)
        if isinstance(config, dict) and "extra" in config:
            assert config["extra"] == "forbid", name
