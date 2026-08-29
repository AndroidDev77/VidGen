"""T25 acceptance: the two scenarios that decide whether the publisher is sound.

The first walks the whole pipeline through an interruption and proves that
exactly one YouTube video is created, that the caption and thumbnail are
uploaded once, that the video stays private until an explicit action, and that
retrying every command creates nothing new.

The second makes the outcome genuinely unknowable and proves the pipeline stops
and asks a human rather than uploading a second copy.

Both run against the deterministic fake provider. Neither makes a network
request, and neither needs a YouTube project, a credential or FFmpeg.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401 - completes Base.metadata
from services.publisher import youtube as capabilities
from services.publisher.credentials import Keyring
from services.publisher.fake_youtube import FakeYouTubeProvider, FakeYouTubeState
from services.publisher.oauth import YouTubeOAuthService
from services.publisher.pipeline import (
    PublicationError,
    PublicationOptions,
    PublicationPipeline,
)
from services.publisher.processing import ProcessingPoller
from tests.publication_fixtures import (
    OAUTH_SETTINGS,
    build_publishable_project,
    connect_fake_channel,
)
from vidgen.contracts.publication import (
    PrivacyState,
    ProcessingState,
    PublicationAssetKind,
    PublicationAssetStatus,
    PublicationFailureCode,
    PublicationStatus,
)
from vidgen.db.base import Base
from vidgen.db.publication_models import (
    PublicationAsset,
    PublicationRun,
    YouTubeUploadSession,
)
from vidgen.db.publication_repository import PublicationRepository
from vidgen.storage.blob import FilesystemBlobStore

CHUNK = capabilities.RESUMABLE_CHUNK_GRANULARITY


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'acceptance.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs", b"test-secret")


def _pipeline(
    session: Session,
    store: FilesystemBlobStore,
    state: FakeYouTubeState,
    keyring: Keyring,
    *,
    max_chunks_per_drive: int | None = None,
) -> PublicationPipeline:
    provider = FakeYouTubeProvider(state)
    repository = PublicationRepository(session, keyring)
    return PublicationPipeline(
        session,
        store,
        provider,
        keyring=keyring,
        oauth=YouTubeOAuthService(repository, provider, OAUTH_SETTINGS),
        options=PublicationOptions(
            chunk_bytes=CHUNK,
            max_processing_polls=6,
            max_chunks_per_drive=max_chunks_per_drive,
        ),
        poller=ProcessingPoller(provider, initial_seconds=0.0, max_seconds=0.0, sleep=_no_sleep),
    )


def test_a_publication_survives_an_interruption_and_creates_exactly_one_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    state = FakeYouTubeState()

    # 1. An approved, current, T22-passing render is selected, and 2. a fake
    #    YouTube channel is connected.
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, keyring = connect_fake_channel(session, state=state)
        # The first worker is bounded to two chunks: it will be stopped mid-upload.
        first_worker = _pipeline(session, store, state, keyring, max_chunks_per_drive=2)
        run = first_worker.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:acceptance",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        run_id = run.id

        # 3. A resumable session is created and persisted, and 4. several chunks
        #    upload successfully.
        asyncio.run(first_worker.start(run))
        checkpoint = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run_id)
        )
        assert checkpoint is not None
        assert checkpoint.status == "active"
        assert 0 < checkpoint.confirmed_offset < checkpoint.total_bytes
        assert run.video_id is None
        interrupted_at = checkpoint.confirmed_offset

    # 5. The worker is interrupted: a new session, a new pipeline, no memory of
    #    the first worker's progress beyond what is durable.
    with factory() as session:
        run = session.get(PublicationRun, run_id)
        assert run is not None
        second_worker = _pipeline(session, store, state, keyring)

        # 6. The fake server reports the confirmed offset, and 7. the upload
        #    resumes from it rather than from byte zero.
        result = asyncio.run(second_worker.resume(run))
        checkpoint = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run_id)
        )
        assert checkpoint is not None
        assert checkpoint.confirmed_offset == checkpoint.total_bytes >= interrupted_at

        # 8. Exactly one fake YouTube video is created.
        assert len(state.videos) == 1
        assert state.count("videos.insert.initialize") == 1
        assert result.video_id

        # 9. Processing succeeds.
        assert result.processing_state is ProcessingState.SUCCEEDED

        # 10. The canonical caption is uploaded once, and 11. so is the thumbnail.
        assert state.count("captions.insert") == 1
        assert state.count("thumbnails.set") == 1
        video = next(iter(state.videos.values()))
        assert len(video.captions) == 1
        assert video.thumbnail_sha256

        # 12. The video remains private.
        assert result.status is PublicationStatus.PRIVATE_READY
        assert result.actual_privacy is PrivacyState.PRIVATE
        assert video.privacy_status == capabilities.PrivacyStatus.PRIVATE.value
        assert result.notify_subscribers is False
        assert result.contains_synthetic_media is True

        # 13. An explicit visibility action changes it to unlisted.
        published = asyncio.run(
            second_worker.apply_visibility(
                run, privacy=PrivacyState.UNLISTED, actor=fixture.owner_subject
            )
        )
        assert published.status is PublicationStatus.PUBLISHED
        assert published.actual_privacy is PrivacyState.UNLISTED

        # 14. Retrying every command creates no duplicate resources.
        before = dict(state.calls)
        again = second_worker.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:acceptance",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        assert again.id == run_id
        asyncio.run(second_worker.start(again))
        asyncio.run(
            second_worker.apply_visibility(
                run, privacy=PrivacyState.UNLISTED, actor=fixture.owner_subject
            )
        )
        assert session.query(PublicationRun).count() == 1
        assert session.query(YouTubeUploadSession).count() == 1
        assert (
            session.query(PublicationAsset)
            .filter(PublicationAsset.kind == PublicationAssetKind.VIDEO.value)
            .count()
            == 1
        )
        assert (
            session.query(PublicationAsset)
            .filter(PublicationAsset.kind == PublicationAssetKind.CAPTION.value)
            .count()
            == 1
        )
        assert (
            session.query(PublicationAsset)
            .filter(PublicationAsset.kind == PublicationAssetKind.THUMBNAIL.value)
            .count()
            == 1
        )
    assert len(state.videos) == 1
    assert state.count("videos.insert.initialize") == before["videos.insert.initialize"]
    assert state.count("captions.insert") == before["captions.insert"]
    assert state.count("thumbnails.set") == before["thumbnails.set"]


def test_an_ambiguous_completion_asks_a_human_instead_of_uploading_again(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, state, keyring = connect_fake_channel(session)
        # The final chunk lands, its response is lost, and the session then
        # disappears: nothing can prove whether a video exists.
        state.ambiguous_completion = True
        pipeline = _pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:ambiguous",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))

        assert result.status is PublicationStatus.HUMAN_REVIEW_REQUIRED
        assert result.failure is not None
        assert result.failure.code is PublicationFailureCode.AMBIGUOUS_COMPLETION
        assert result.video_id is None

        # The evidence is preserved rather than discarded.
        checkpoint = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )
        assert checkpoint is not None
        assert checkpoint.status == "ambiguous"
        assert run.review_reason
        assert checkpoint.session_uri_hash[:16] in run.review_reason

        initializations = state.count("videos.insert.initialize")

        # Resuming refuses. No second session, and no second video.
        with pytest.raises(PublicationError) as refusal:
            asyncio.run(pipeline.resume(run))
        assert refusal.value.failure.code is PublicationFailureCode.AMBIGUOUS_COMPLETION
        session.rollback()
        assert state.count("videos.insert.initialize") == initializations
        assert session.query(YouTubeUploadSession).count() == 1
        assert (
            session.query(PublicationAsset)
            .filter(
                PublicationAsset.kind == PublicationAssetKind.VIDEO.value,
                PublicationAsset.status == PublicationAssetStatus.SUCCEEDED.value,
            )
            .count()
            == 0
        )
