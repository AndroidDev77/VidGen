"""Temporal activities for T25 YouTube publication.

Every activity takes and returns an ID-only contract: a project ID, a
publication-run ID, a connection ID, a render asset ID, an idempotency key and a
trace context in, and a status, phase, video ID and confirmed offset out.
Nothing else may cross this boundary - not metadata text, not an OAuth
credential, not a resumable session URI, not caption or thumbnail bytes, and not
a provider payload - because Temporal history is durable, replayable and visible
in the UI.

The handler is installed by the worker process, which owns the database session,
the blob store, the keyring and the provider client. That keeps this module free
of any of them, so importing it in a workflow sandbox is safe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import copy_context
from threading import Event, Thread

from temporalio import activity

from vidgen.contracts.publication import PublicationActivityInput, PublicationActivityResult

PublicationHandler = Callable[[str, PublicationActivityInput], PublicationActivityResult]

_handler: dict[str, PublicationHandler] = {}

#: A resumable upload chunk can take minutes on a slow link. Heartbeating keeps
#: Temporal from treating a healthy long upload as a dead worker, and is what
#: lets a cancellation reach the uploader between chunks.
HEARTBEAT_INTERVAL_SECONDS = 20.0


def configure_publication_handler(handler: PublicationHandler | None) -> None:
    """Install the process-local publisher adapter."""
    _handler.clear()
    if handler is not None:
        _handler["publication"] = handler


@contextmanager
def _heartbeats(step: str) -> Iterator[None]:
    stopped = Event()

    def beat() -> None:
        while not stopped.wait(HEARTBEAT_INTERVAL_SECONDS):
            activity.heartbeat({"step": step})

    activity.heartbeat({"step": step})
    context = copy_context()
    thread = Thread(target=context.run, args=(beat,), name=f"heartbeat-{step}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


def _execute(step: str, request: PublicationActivityInput) -> PublicationActivityResult:
    handler = _handler.get("publication")
    if handler is None:
        raise RuntimeError("no publication activity handler configured")
    with _heartbeats(step):
        return handler(step, request)


@activity.defn(name="validate_publication_eligibility_activity")
def validate_publication_eligibility_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("validate_eligibility", request)


@activity.defn(name="refresh_publication_connection_activity")
def refresh_publication_connection_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("refresh_connection", request)


@activity.defn(name="initialize_publication_upload_activity")
def initialize_publication_upload_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("initialize_upload", request)


@activity.defn(name="upload_publication_chunks_activity")
def upload_publication_chunks_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("upload_chunks", request)


@activity.defn(name="poll_publication_processing_activity")
def poll_publication_processing_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("poll_processing", request)


@activity.defn(name="upload_publication_captions_activity")
def upload_publication_captions_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("upload_captions", request)


@activity.defn(name="upload_publication_thumbnail_activity")
def upload_publication_thumbnail_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("upload_thumbnail", request)


@activity.defn(name="verify_publication_private_activity")
def verify_publication_private_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("verify_private", request)


@activity.defn(name="apply_publication_visibility_activity")
def apply_publication_visibility_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("apply_visibility", request)


@activity.defn(name="finalize_publication_activity")
def finalize_publication_activity(
    request: PublicationActivityInput,
) -> PublicationActivityResult:
    return _execute("finalize", request)


PUBLICATION_ACTIVITIES = [
    validate_publication_eligibility_activity,
    refresh_publication_connection_activity,
    initialize_publication_upload_activity,
    upload_publication_chunks_activity,
    poll_publication_processing_activity,
    upload_publication_captions_activity,
    upload_publication_thumbnail_activity,
    verify_publication_private_activity,
    apply_publication_visibility_activity,
    finalize_publication_activity,
]
