"""T25 Temporal integration: ID-only messages, a dedicated queue, and resume.

The workflow is exercised against a recording handler rather than a Temporal
server, so the step ordering and the message shape are asserted without a
cluster. The publisher worker's own configuration is asserted from its module.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import vidgen.db  # noqa: F401 - completes Base.metadata
from packages.workflows.publication import (
    PUBLISHER_TASK_QUEUE,
    YouTubePublicationWorkflow,
)
from packages.workflows.publication_activities import (
    PUBLICATION_ACTIVITIES,
    configure_publication_handler,
)
from tests.publication_fixtures import build_publishable_project, connect_fake_channel
from vidgen.contracts.publication import (
    PublicationActivityInput,
    PublicationActivityResult,
    PublicationPhase,
    PublicationStatus,
)
from vidgen.db.base import Base
from vidgen.review.workflow_control import (
    TASK_QUEUE,
    FakeWorkflowController,
    publication_workflow_id,
)
from vidgen.storage.blob import FilesystemBlobStore


def message() -> PublicationActivityInput:
    return PublicationActivityInput(
        project_id=uuid4(),
        publication_run_id=uuid4(),
        connection_id=uuid4(),
        final_render_asset_id=uuid4(),
        idempotency_key="publish:1",
        trace_context={"traceparent": "00-abc-def-01"},
    )


def test_the_activity_message_is_ids_and_nothing_else() -> None:
    payload = message().model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "project_id",
        "publication_run_id",
        "connection_id",
        "final_render_asset_id",
        "idempotency_key",
        "trace_context",
    }
    serialized = json.dumps(payload).lower()
    for forbidden in ("token", "secret", "https://", "caption", "thumbnail", "title"):
        assert forbidden not in serialized


def test_the_activity_message_bounds_its_trace_context() -> None:
    with pytest.raises(ValueError, match="bounded"):
        PublicationActivityInput(
            project_id=uuid4(),
            publication_run_id=uuid4(),
            connection_id=uuid4(),
            final_render_asset_id=uuid4(),
            idempotency_key="k",
            trace_context={str(index): "x" for index in range(9)},
        )


def test_the_activity_result_is_a_bounded_projection() -> None:
    result = PublicationActivityResult(
        publication_run_id=uuid4(),
        status=PublicationStatus.UPLOADING,
        phase=PublicationPhase.MEDIA_UPLOAD,
        confirmed_offset=1024,
        total_bytes=4096,
    )
    payload = result.model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "publication_run_id",
        "status",
        "phase",
        "video_id",
        "confirmed_offset",
        "total_bytes",
        "processing_state",
        "failure_code",
        "retryable",
    }


def test_the_publisher_uses_a_dedicated_task_queue() -> None:
    assert PUBLISHER_TASK_QUEUE == "vidgen-publisher"
    # Deliberately not the ordinary project queue: a multi-hour upload must not
    # occupy a slot that every other project's activities compete for.
    assert PUBLISHER_TASK_QUEUE != TASK_QUEUE


def test_every_declared_activity_has_a_registered_definition() -> None:
    names = {activity.__name__ for activity in PUBLICATION_ACTIVITIES}
    assert names == {
        "validate_publication_eligibility_activity",
        "refresh_publication_connection_activity",
        "initialize_publication_upload_activity",
        "upload_publication_chunks_activity",
        "poll_publication_processing_activity",
        "upload_publication_captions_activity",
        "upload_publication_thumbnail_activity",
        "verify_publication_private_activity",
        "apply_publication_visibility_activity",
        "finalize_publication_activity",
    }


def test_an_unconfigured_handler_fails_loudly() -> None:
    from packages.workflows.publication_activities import _execute

    configure_publication_handler(None)
    with pytest.raises(RuntimeError, match="no publication activity handler"):
        _execute("validate_eligibility", message())


def test_the_workflow_stops_at_a_verified_private_video() -> None:
    """Driven with a recording stub in place of Temporal's activity dispatch."""
    calls: list[str] = []
    workflow = YouTubePublicationWorkflow()
    request = message()
    run_id = request.publication_run_id

    async def step(
        name: str, req: PublicationActivityInput, start_to_close: object
    ) -> PublicationActivityResult:
        calls.append(name)
        if name == "upload_publication_chunks_activity":
            return PublicationActivityResult(
                publication_run_id=run_id,
                status=PublicationStatus.PROCESSING,
                phase=PublicationPhase.PROCESSING_POLL,
                video_id="vid1",
            )
        if name == "poll_publication_processing_activity":
            return PublicationActivityResult(
                publication_run_id=run_id,
                status=PublicationStatus.UPLOADING_CAPTIONS,
                phase=PublicationPhase.CAPTIONS,
                video_id="vid1",
            )
        if name in {
            "upload_publication_captions_activity",
            "upload_publication_thumbnail_activity",
            "verify_publication_private_activity",
            "finalize_publication_activity",
        }:
            return PublicationActivityResult(
                publication_run_id=run_id,
                status=PublicationStatus.PRIVATE_READY,
                phase=PublicationPhase.VERIFICATION,
                video_id="vid1",
            )
        return PublicationActivityResult(
            publication_run_id=run_id,
            status=PublicationStatus.READY,
            phase=PublicationPhase.AUTHORIZATION,
        )

    workflow._step = step  # type: ignore[assignment]
    result = asyncio.run(workflow.run(request))
    assert calls == [
        "validate_publication_eligibility_activity",
        "refresh_publication_connection_activity",
        "initialize_publication_upload_activity",
        "upload_publication_chunks_activity",
        "poll_publication_processing_activity",
        "upload_publication_captions_activity",
        "upload_publication_thumbnail_activity",
        "verify_publication_private_activity",
        "finalize_publication_activity",
    ]
    # The workflow never makes the video visible on its own.
    assert "apply_publication_visibility_activity" not in calls
    assert result.status is PublicationStatus.PRIVATE_READY


def test_the_workflow_stops_on_a_waiting_state_without_uploading() -> None:
    calls: list[str] = []
    workflow = YouTubePublicationWorkflow()
    request = message()

    async def step(
        name: str, req: PublicationActivityInput, start_to_close: object
    ) -> PublicationActivityResult:
        calls.append(name)
        return PublicationActivityResult(
            publication_run_id=request.publication_run_id,
            status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
            phase=PublicationPhase.ELIGIBILITY,
        )

    workflow._step = step  # type: ignore[assignment]
    result = asyncio.run(workflow.run(request))
    assert calls == ["validate_publication_eligibility_activity"]
    assert result.status is PublicationStatus.HUMAN_REVIEW_REQUIRED


def test_the_workflow_re_enters_the_upload_until_a_video_exists() -> None:
    calls: list[str] = []
    workflow = YouTubePublicationWorkflow()
    request = message()
    offsets = iter([1024, 2048, 4096])

    async def step(
        name: str, req: PublicationActivityInput, start_to_close: object
    ) -> PublicationActivityResult:
        calls.append(name)
        if name == "upload_publication_chunks_activity":
            offset = next(offsets)
            return PublicationActivityResult(
                publication_run_id=request.publication_run_id,
                status=PublicationStatus.UPLOADING,
                phase=PublicationPhase.MEDIA_UPLOAD,
                confirmed_offset=offset,
                total_bytes=4096,
                video_id="vid1" if offset == 4096 else None,
            )
        if name == "initialize_publication_upload_activity":
            return PublicationActivityResult(
                publication_run_id=request.publication_run_id,
                status=PublicationStatus.UPLOADING,
                phase=PublicationPhase.MEDIA_UPLOAD,
                total_bytes=4096,
            )
        return PublicationActivityResult(
            publication_run_id=request.publication_run_id,
            status=PublicationStatus.PRIVATE_READY,
            phase=PublicationPhase.VERIFICATION,
            video_id="vid1",
        )

    workflow._step = step  # type: ignore[assignment]
    asyncio.run(workflow.run(request))
    assert calls.count("upload_publication_chunks_activity") == 3
    # One video, one initialization: the workflow never re-initializes.
    assert calls.count("initialize_publication_upload_activity") == 1


def test_a_repeated_start_adopts_the_existing_workflow() -> None:
    controller = FakeWorkflowController()
    request = message()
    first = controller.start_publication(request)
    second = controller.start_publication(request)
    assert first == second
    assert controller.publication_start_calls == 2
    assert len(controller.publications) == 1
    assert first[0] == publication_workflow_id(request.publication_run_id)


def test_the_worker_handler_returns_only_ids(tmp_path: Path) -> None:
    from workers.youtube_publisher.handlers import build_publication_handler

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FilesystemBlobStore(tmp_path / "blobs", b"test-secret")
    with factory() as session:
        fixture = build_publishable_project(session, store)
        connection, _, _ = connect_fake_channel(session)
        from services.publisher.commands import PublisherCommandOptions, build_pipeline
        from tests.publication_fixtures import OAUTH_SETTINGS

        pipeline = build_pipeline(
            session,
            store,
            PublisherCommandOptions(provider="fake"),
            oauth_settings=OAUTH_SETTINGS,
        )
        run = pipeline.create_draft(
            project_id=fixture.project_id,
            owner_subject=fixture.owner_subject,
            connection_id=connection.id,
            idempotency_key="publish:1",
        )
        session.commit()
        run_id = run.id

    handler = build_publication_handler(factory, store)
    result = handler(
        "validate_eligibility",
        PublicationActivityInput(
            project_id=fixture.project_id,
            publication_run_id=run_id,
            connection_id=connection.id,
            final_render_asset_id=fixture.final_asset_id,
            idempotency_key="publish:1",
        ),
    )
    assert isinstance(result, PublicationActivityResult)
    assert result.publication_run_id == run_id
    assert result.status is PublicationStatus.READY
    serialized = json.dumps(result.model_dump(mode="json")).lower()
    for forbidden in ("token", "secret", "https://"):
        assert forbidden not in serialized


def test_the_publisher_worker_is_configured_for_graceful_shutdown() -> None:
    source = Path("workers/youtube_publisher/main.py").read_text()
    assert "graceful_shutdown_timeout" in source
    assert "signal.SIGTERM" in source
    # No ingress: the worker exposes no port and no HTTP server.
    assert "uvicorn" not in source
    assert "VIDGEN_PUBLISHER_MAX_CONCURRENT_UPLOADS" in source
    assert "PUBLISHER_TASK_QUEUE" in source
