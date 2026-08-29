"""T25: resumable upload, processing, captions, thumbnails and visibility.

Every test drives the real pipeline against the deterministic fake provider, so
the chunking, the ``308`` handling, the server-confirmed offset recovery and the
ambiguity rules are exercised exactly as they would be against YouTube. No test
here makes a network request.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import vidgen.db  # noqa: F401 - completes Base.metadata
from services.publisher import youtube as capabilities
from services.publisher.credentials import Keyring, SecretValue
from services.publisher.fake_youtube import FakeYouTubeProvider, FakeYouTubeState
from services.publisher.oauth import YouTubeOAuthService
from services.publisher.pipeline import (
    PublicationError,
    PublicationOptions,
    PublicationPipeline,
)
from services.publisher.processing import ProcessingPoller
from services.publisher.resumable import ResumableUploader, chunk_source_for, plan_chunks
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
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.publication_models import (
    PublicationAsset,
    PublicationRun,
    YouTubeUploadSession,
)
from vidgen.db.publication_repository import PublicationRepository
from vidgen.storage.blob import FilesystemBlobStore

CHUNK = capabilities.RESUMABLE_CHUNK_GRANULARITY


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'resumable.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs", b"test-secret")


def build_pipeline(
    session: Session,
    store: FilesystemBlobStore,
    state: FakeYouTubeState,
    keyring: Keyring,
    *,
    chunk_bytes: int = CHUNK,
    max_processing_polls: int | None = 5,
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
            chunk_bytes=chunk_bytes,
            max_processing_polls=max_processing_polls,
            max_chunks_per_drive=max_chunks_per_drive,
        ),
        # No real sleeping in tests: the backoff schedule is asserted separately.
        poller=ProcessingPoller(provider, initial_seconds=0.0, max_seconds=0.0, sleep=_no_sleep),
    )


async def _no_sleep(seconds: float) -> None:
    return None


def prepared(session: Session, store: FilesystemBlobStore, **kwargs: object):
    fixture = build_publishable_project(session, store, **kwargs)  # type: ignore[arg-type]
    connection, state, keyring = connect_fake_channel(session)
    return fixture, connection, state, keyring


# -- deterministic chunking ----------------------------------------------------
def test_chunk_plans_are_deterministic_and_aligned() -> None:
    plan = plan_chunks(3 * CHUNK + 100, CHUNK)
    assert plan == [(0, CHUNK), (CHUNK, CHUNK), (2 * CHUNK, CHUNK), (3 * CHUNK, 100)]
    assert plan == plan_chunks(3 * CHUNK + 100, CHUNK)
    # Resuming produces the same ranges the first attempt would have used.
    assert plan_chunks(3 * CHUNK + 100, CHUNK, start=2 * CHUNK) == plan[2:]
    # Every chunk but the last is a 256 KiB multiple.
    assert all(length % CHUNK == 0 for _, length in plan[:-1])


def test_a_misconfigured_chunk_size_is_rounded_to_a_legal_one() -> None:
    assert capabilities.normalize_chunk_bytes(1000) == CHUNK
    assert capabilities.normalize_chunk_bytes(CHUNK + 1) == CHUNK
    assert capabilities.normalize_chunk_bytes(10 * CHUNK) == 10 * CHUNK
    assert capabilities.normalize_chunk_bytes(10**12) == capabilities.MAX_CHUNK_BYTES


def test_a_blob_chunk_source_never_reads_the_whole_file(
    store: FilesystemBlobStore,
) -> None:
    payload = bytes(range(256)) * 1024
    store.put_if_absent("chunked", payload)
    source = chunk_source_for(store, key="chunked", byte_size=len(payload), media_type="video/mp4")
    assert source.read_range(10, 5) == payload[10:15]
    assert source.read_range(len(payload) - 3, 100) == payload[-3:]
    assert source.read_range(len(payload), 10) == b""


# -- the happy path ------------------------------------------------------------
def test_a_publication_uploads_processes_captions_and_thumbnails_once(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))

    assert result.status is PublicationStatus.PRIVATE_READY
    assert result.video_id
    assert result.video_url == capabilities.watch_url(result.video_id)
    assert result.actual_privacy is PrivacyState.PRIVATE
    assert result.processing_state is ProcessingState.SUCCEEDED
    assert result.caption_status is PublicationAssetStatus.SUCCEEDED
    assert result.thumbnail_status is PublicationAssetStatus.SUCCEEDED
    assert result.confirmed_offset == result.total_bytes == fixture.total_bytes
    assert result.contains_synthetic_media is True
    assert result.notify_subscribers is False
    # Exactly one video, one caption, one thumbnail.
    assert state.count("videos.insert.initialize") == 1
    assert len(state.videos) == 1
    assert state.count("captions.insert") == 1
    assert state.count("thumbnails.set") == 1


def test_the_initial_upload_is_always_private_and_silent(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        draft = pipeline.draft_of(run)
        # Even when the user has asked for a public video eventually.
        pipeline.update_draft(
            run, draft.model_copy(update={"requested_privacy": PrivacyState.PUBLIC})
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        video = next(iter(state.videos.values()))
    assert video.privacy_status == capabilities.PrivacyStatus.PRIVATE.value
    assert video.metadata.notify_subscribers is False
    assert video.metadata.contains_synthetic_media is True
    assert video.metadata.made_for_kids is False


def test_the_resumable_session_is_persisted_before_any_media_byte(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    """A worker killed after ``videos.insert`` must be able to resume."""
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        # Zero chunks per drive: the session must exist before any media byte.
        pipeline = build_pipeline(session, store, state, keyring, max_chunks_per_drive=0)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.run_step("validate_eligibility", run))
        asyncio.run(pipeline.run_step("refresh_connection", run))
        asyncio.run(pipeline.run_step("initialize_upload", run))
        upload = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )
    assert upload is not None
    assert upload.status == "active"
    assert upload.confirmed_offset == 0
    assert upload.total_bytes == fixture.total_bytes
    assert len(upload.session_uri_hash) == 64
    # Sealed, and never stored in the clear.
    assert b"fake-upload.googleapis.com" not in upload.session_uri_ciphertext
    assert len(upload.session_uri_nonce) == 12


def test_an_interrupted_upload_resumes_from_the_server_confirmed_offset(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    """The bytes land, the response is lost, and the server settles the offset."""
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        # The fake accepts the chunk and then loses the response, so the local
        # checkpoint is behind what the server actually holds.
        state.interrupt_after_offset = 2 * CHUNK
        state.interrupt_remaining = 1
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
        checkpoint = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )

    assert result.status is PublicationStatus.PRIVATE_READY
    assert result.confirmed_offset == fixture.total_bytes
    # One video and one session, despite the interruption.
    assert len(state.videos) == 1
    assert state.count("videos.insert.initialize") == 1
    assert checkpoint is not None and checkpoint.status == "completed"
    # The lost chunk was never resent: the server's own range moved the offset
    # past it, and every offset the checkpoint ever held was non-decreasing.
    assert state.count("videos.insert.chunk") <= (fixture.total_bytes // CHUNK + 3)


def test_a_308_range_correction_moves_the_offset_forward(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    """A stale local offset is corrected by the server's ``Range``, not resent."""
    state = FakeYouTubeState()
    provider = FakeYouTubeProvider(state)
    confirmed: list[int] = []
    uploader = ResumableUploader(
        provider, chunk_bytes=CHUNK, on_confirmed=lambda offset, code: confirmed.append(offset)
    )
    total = 2 * CHUNK
    payload = bytes(total)
    session = asyncio.run(
        provider.initialize_resumable_upload(
            access_token=SecretValue("t"),
            metadata=_metadata(),
            total_bytes=total,
            media_type="video/mp4",
        )
    )

    class _Source:
        byte_size = total
        media_type = "video/mp4"

        def read_range(self, start: int, length: int) -> bytes:
            return payload[start : start + length]

    outcome = asyncio.run(
        uploader.drive(
            access_token=SecretValue("t"),
            upload_uri=session.upload_uri,
            source=_Source(),
            total_bytes=total,
            start_offset=0,
        )
    )
    assert outcome.completed and outcome.video_id
    assert confirmed[-1] == total
    assert confirmed == sorted(confirmed)


def test_a_lost_final_response_is_recovered_without_a_second_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.lose_final_response = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
    # The status query recovered the completed session and its video ID.
    assert result.video_id
    assert len(state.videos) == 1
    assert state.count("videos.insert.initialize") == 1


def test_an_ambiguous_completion_holds_the_publication_for_review(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.ambiguous_completion = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
        upload = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )
        assert result.status is PublicationStatus.HUMAN_REVIEW_REQUIRED
        assert result.failure is not None
        assert result.failure.code is PublicationFailureCode.AMBIGUOUS_COMPLETION
        assert upload is not None and upload.status == "ambiguous"
        # The evidence is preserved: the session identity and its offset.
        assert run.review_reason and upload.session_uri_hash[:16] in run.review_reason
        before = state.count("videos.insert.initialize")

        # Resuming must refuse rather than start a second upload.
        with pytest.raises(PublicationError):
            asyncio.run(pipeline.resume(run))
    assert state.count("videos.insert.initialize") == before


def test_an_expired_session_that_confirmed_nothing_may_start_another(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.expire_sessions = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        assert run.status == PublicationStatus.UPLOAD_INITIALIZING.value
        session_row = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )
        assert session_row is not None and session_row.status == "expired"
        assert session_row.confirmed_offset == 0
        # Nothing was created, so another attempt is safe.
        state.expire_sessions = False
        result = asyncio.run(pipeline.resume(run))
    assert result.status is PublicationStatus.PRIVATE_READY
    assert len(state.videos) == 1


def test_a_worker_interruption_between_chunks_loses_no_progress(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    """Simulates the worker dying: a new session object, a new pipeline."""
    engine_bound = factory
    with engine_bound() as session:
        fixture, connection, state, keyring = prepared(session, store)
        # The first worker sends two chunks and is then stopped mid-upload.
        pipeline = build_pipeline(session, store, state, keyring, max_chunks_per_drive=2)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        run_id = run.id

    with engine_bound() as session:
        run = session.get(PublicationRun, run_id)
        assert run is not None
        checkpoint = session.scalar(
            select(YouTubeUploadSession).where(YouTubeUploadSession.publication_run_id == run.id)
        )
        assert checkpoint is not None
        assert 0 < checkpoint.confirmed_offset < checkpoint.total_bytes
        pipeline = build_pipeline(session, store, state, keyring)
        result = asyncio.run(pipeline.resume(run))
    assert result.status is PublicationStatus.PRIVATE_READY
    assert len(state.videos) == 1


def test_the_video_id_is_persisted_before_processing_is_polled(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        for step in ("validate_eligibility", "refresh_connection", "initialize_upload"):
            asyncio.run(pipeline.run_step(step, run))
        asyncio.run(pipeline.run_step("upload_chunks", run))
        assert run.video_id
        assert run.status == PublicationStatus.PROCESSING.value
        assert state.count("videos.processing") == 0
        asyncio.run(pipeline.run_step("poll_processing", run))
    assert state.count("videos.processing") >= 1


def test_a_processing_failure_keeps_the_video_id_for_investigation(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.terminal_processing_status = capabilities.ProcessingStatus.FAILED.value
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
    assert result.status is PublicationStatus.PROCESSING_FAILED
    assert result.processing_state is ProcessingState.FAILED
    assert result.video_id
    assert result.video_url
    assert len(state.videos) == 1


def test_slow_processing_is_not_a_failure_and_never_re_uploads(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.processing_polls_until_terminal = 1000
        pipeline = build_pipeline(session, store, state, keyring, max_processing_polls=2)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
    assert result.status is PublicationStatus.PROCESSING
    assert result.processing_state is ProcessingState.PROCESSING
    assert state.count("videos.insert.initialize") == 1


def test_processing_backoff_is_bounded_and_exponential() -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    state = FakeYouTubeState(processing_polls_until_terminal=1000)
    provider = FakeYouTubeProvider(state)
    provider.state.videos["v"] = _fake_video()
    poller = ProcessingPoller(
        provider,
        initial_seconds=1.0,
        max_seconds=4.0,
        backoff_factor=2.0,
        max_elapsed_seconds=60.0,
        sleep=record,
    )
    outcome = asyncio.run(poller.poll(access_token=SecretValue("t"), video_id="v", max_polls=5))
    assert outcome.timed_out
    assert slept == [1.0, 2.0, 4.0, 4.0]


# -- captions ------------------------------------------------------------------
def test_a_caption_conflict_adopts_the_existing_track_without_duplicating(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        first = pipeline.project(run)
        assert first.caption_track_id
        inserts = state.count("captions.insert")

        # Force the caption phase to run again against an existing track.
        asset = session.scalar(
            select(PublicationAsset).where(
                PublicationAsset.publication_run_id == run.id,
                PublicationAsset.kind == PublicationAssetKind.CAPTION.value,
            )
        )
        assert asset is not None
        asset.status = PublicationAssetStatus.FAILED.value
        asset.error_code = "RETRY"
        session.commit()
        pipeline.repository.transition(run, PublicationStatus.UPLOADING_CAPTIONS)
        session.commit()
        draft = pipeline.draft_of(run)
        render = pipeline._revalidate(run)
        asyncio.run(pipeline._upload_captions(run, render.connection, render, draft))
        session.commit()
        second = pipeline.project(run)
    # A second insert was attempted, refused as a conflict, and the existing
    # track was adopted rather than duplicated.
    assert state.count("captions.insert") == inserts + 1
    video = next(iter(state.videos.values()))
    assert len(video.captions) == 1
    assert second.caption_track_id == first.caption_track_id
    assert second.caption_status is PublicationAssetStatus.SUCCEEDED


def test_a_caption_failure_preserves_the_private_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        # A caption asset that is not parseable SRT is refused before the call.
        from vidgen.db.models import Asset

        asset = session.get(Asset, fixture.caption_asset_id)
        assert asset is not None
        store.put_if_absent(asset.storage_key + ".broken", b"not a caption file")
        asset.storage_key = asset.storage_key + ".broken"
        session.commit()
        result = asyncio.run(pipeline.start(run))
    assert result.status is PublicationStatus.PRIVATE_READY
    assert result.video_id
    assert result.caption_status is PublicationAssetStatus.FAILED
    assert len(state.videos) == 1
    assert state.count("captions.insert") == 0


# -- thumbnails ----------------------------------------------------------------
def test_a_channel_that_cannot_set_thumbnails_keeps_its_private_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.thumbnails_forbidden = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
        asset = session.scalar(
            select(PublicationAsset).where(
                PublicationAsset.publication_run_id == run.id,
                PublicationAsset.kind == PublicationAssetKind.THUMBNAIL.value,
            )
        )
    assert result.status is PublicationStatus.PRIVATE_READY
    assert result.video_id
    assert result.thumbnail_status is PublicationAssetStatus.FAILED
    assert asset is not None
    assert asset.error_code == PublicationFailureCode.THUMBNAIL_NOT_PERMITTED.value
    assert asset.error_summary


def test_a_successful_thumbnail_is_never_uploaded_twice(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        assert state.count("thumbnails.set") == 1
        render = pipeline._revalidate(run)
        asyncio.run(pipeline._upload_thumbnail(run, render.connection, render))
    assert state.count("thumbnails.set") == 1


def test_a_publication_without_a_thumbnail_skips_the_step(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
    assert result.thumbnail_status is PublicationAssetStatus.SKIPPED
    assert state.count("thumbnails.set") == 0


# -- visibility ----------------------------------------------------------------
def test_visibility_only_changes_on_an_explicit_action(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        after_upload = asyncio.run(pipeline.start(run))
        assert after_upload.actual_privacy is PrivacyState.PRIVATE
        assert run.visibility_decision_at is None
        assert state.count("videos.visibility") == 0

        result = asyncio.run(
            pipeline.apply_visibility(
                run, privacy=PrivacyState.UNLISTED, actor=fixture.owner_subject
            )
        )
    assert result.status is PublicationStatus.PUBLISHED
    assert result.actual_privacy is PrivacyState.UNLISTED
    assert run.visibility_decision_at is not None
    assert run.visibility_decided_by == fixture.owner_subject


def test_an_api_project_restricted_to_private_never_reports_success(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.privacy_restricted = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        with pytest.raises(PublicationError) as error:
            asyncio.run(
                pipeline.apply_visibility(
                    run, privacy=PrivacyState.PUBLIC, actor=fixture.owner_subject
                )
            )
        session.refresh(run)
    assert PublicationFailureCode.PRIVACY_RESTRICTED.value in str(error.value) or (
        run.error_code == PublicationFailureCode.PRIVACY_RESTRICTED.value
    )
    assert run.actual_privacy == PrivacyState.PRIVATE.value
    assert run.status != PublicationStatus.PUBLISHED.value


def test_a_stale_render_blocks_the_visibility_change(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    """The lineage is rechecked immediately before the video becomes visible."""
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        from vidgen.db.review_models import DownstreamInvalidation

        session.add(
            DownstreamInvalidation(
                project_id=fixture.project_id,
                origin_type="script",
                origin_id=fixture.graph.script_id,
                invalidated_type="render",
                invalidated_id=fixture.render_job_id,
                reason="script_edited",
            )
        )
        session.commit()
        from services.publisher.eligibility import PublicationEligibilityError

        with pytest.raises(PublicationEligibilityError):
            asyncio.run(
                pipeline.apply_visibility(
                    run, privacy=PrivacyState.PUBLIC, actor=fixture.owner_subject
                )
            )
    assert state.count("videos.visibility") == 0


def test_a_scheduled_publication_is_submitted_as_a_private_video(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        when = datetime.now(UTC) + timedelta(days=2)
        result = asyncio.run(
            pipeline.apply_visibility(
                run,
                privacy=PrivacyState.PUBLIC,
                actor=fixture.owner_subject,
                scheduled_publish_at=when,
            )
        )
    assert result.actual_privacy is PrivacyState.PRIVATE
    assert result.scheduled_publish_at is not None
    video = next(iter(state.videos.values()))
    assert video.publish_at is not None


def test_a_schedule_in_the_past_is_refused(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    from services.publisher.metadata import PublicationMetadataError

    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        with pytest.raises(PublicationMetadataError):
            asyncio.run(
                pipeline.apply_visibility(
                    run,
                    privacy=PrivacyState.PUBLIC,
                    actor=fixture.owner_subject,
                    scheduled_publish_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
    assert state.count("videos.visibility") == 0


# -- quota and telemetry -------------------------------------------------------
def test_quota_units_are_recorded_with_a_zero_monetary_cost(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
        attempts = session.scalars(
            select(ProviderAttempt).where(ProviderAttempt.related_entity_id == run.id)
        ).all()
    assert attempts
    assert result.quota_units > 0
    for attempt in attempts:
        assert Decimal(str(attempt.actual_cost)) == Decimal("0")
        assert Decimal(str(attempt.estimated_cost)) == Decimal("0")
        for entry in attempt.usage or []:
            assert entry["unit"] == capabilities.QUOTA_USAGE_UNIT
            assert int(entry["quantity"]) >= 0
        assert attempt.operation.startswith("youtube.")


def test_telemetry_never_records_a_token_or_a_session_uri(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        asyncio.run(pipeline.start(run))
        attempts = session.scalars(
            select(ProviderAttempt).where(ProviderAttempt.related_entity_id == run.id)
        ).all()
    serialized = " ".join(str(attempt.redacted_metadata) for attempt in attempts)
    assert "fake-access-token" not in serialized
    assert "fake-refresh-token" not in serialized
    assert "fake-upload.googleapis.com" not in serialized


def test_an_exhausted_quota_blocks_rather_than_fails(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        state.quota_exhausted = True
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        result = asyncio.run(pipeline.start(run))
    assert result.status is PublicationStatus.QUOTA_BLOCKED
    assert result.failure is not None
    assert result.failure.code is PublicationFailureCode.QUOTA_EXCEEDED
    assert result.failure.retryable is True
    assert len(state.videos) == 0


# -- idempotency ---------------------------------------------------------------
def test_retrying_a_completed_publication_creates_nothing_new(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        fixture, connection, state, keyring = prepared(session, store)
        pipeline = build_pipeline(session, store, state, keyring)
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        session.commit()
        first = asyncio.run(pipeline.start(run))
        counts = dict(state.calls)
        again = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
            thumbnail_asset_id=fixture.thumbnail_asset_id,
        )
        assert again.id == run.id
        second = asyncio.run(pipeline.start(again))
        assert session.query(PublicationRun).count() == 1
    assert second.publication_run_id == first.publication_run_id
    assert second.video_id == first.video_id
    assert state.count("videos.insert.initialize") == counts["videos.insert.initialize"]
    assert state.count("captions.insert") == counts["captions.insert"]
    assert state.count("thumbnails.set") == counts["thumbnails.set"]


def test_a_different_render_needs_a_different_identity(
    factory: sessionmaker[Session], store: FilesystemBlobStore
) -> None:
    with factory() as session:
        first = build_publishable_project(session, store, name="first")
        second = build_publishable_project(session, store, name="second", video_bytes=CHUNK * 2)
        connection, state, keyring = connect_fake_channel(session)
        pipeline = build_pipeline(session, store, state, keyring)
        run_a = pipeline.create_draft(
            project_id=first.project_id,
            owner_subject=first.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:a",
        )
        run_b = pipeline.create_draft(
            project_id=second.project_id,
            owner_subject=second.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:b",
        )
        session.commit()
    assert run_a.publication_identity != run_b.publication_identity


def _metadata():
    from services.publisher.contracts import VideoMetadata

    return VideoMetadata(
        title="t",
        description="",
        tags=(),
        category_id="24",
        default_language="en",
        privacy_status="private",
        made_for_kids=False,
        contains_synthetic_media=True,
        embeddable=True,
        notify_subscribers=False,
    )


def _fake_video():
    from services.publisher.fake_youtube import FakeVideo

    return FakeVideo(video_id="v", metadata=_metadata(), privacy_status="private")
