"""The production adapter behind every T25 publication activity.

The worker process owns the database session factory, the blob store, the
credential keyring and the provider client; the activity module owns none of
them. This handler is where those meet, once per activity invocation, inside a
transaction that is committed or rolled back before the activity returns.

The result crossing back into Temporal history is deliberately tiny: a status, a
phase, a video ID, a confirmed offset and a failure classification. No metadata
text, no credential, no session URI and no provider payload.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from services.publisher.commands import PublisherCommandOptions, build_pipeline
from services.publisher.eligibility import PublicationEligibilityError
from services.publisher.oauth import OAuthFlowError
from services.publisher.pipeline import PublicationError
from services.publisher.providers import FAKE_PROVIDER, YOUTUBE_PROVIDER
from services.publisher.youtube import DEFAULT_CHUNK_BYTES, normalize_chunk_bytes
from vidgen.contracts.publication import (
    RETRYABLE_FAILURE_CODES,
    ProcessingState,
    PublicationActivityInput,
    PublicationActivityResult,
    PublicationFailureCode,
    PublicationPhase,
    PublicationStatus,
)
from vidgen.db.publication_models import PublicationRun
from vidgen.storage.blob import BlobStore

logger = logging.getLogger("vidgen.publisher")


def _options() -> PublisherCommandOptions:
    provider = os.getenv("VIDGEN_YOUTUBE_PROVIDER", FAKE_PROVIDER).strip().lower()
    if provider not in {FAKE_PROVIDER, YOUTUBE_PROVIDER}:
        raise RuntimeError(
            f"VIDGEN_YOUTUBE_PROVIDER must be '{FAKE_PROVIDER}' or '{YOUTUBE_PROVIDER}'"
        )
    raw_chunk = os.getenv("VIDGEN_YOUTUBE_UPLOAD_CHUNK_BYTES")
    chunk = normalize_chunk_bytes(int(raw_chunk)) if raw_chunk else DEFAULT_CHUNK_BYTES
    return PublisherCommandOptions(provider=provider, chunk_bytes=chunk)


def _projection(run: PublicationRun, session: Session) -> PublicationActivityResult:
    from services.publisher.projections import latest_session

    upload = latest_session(session, run.id)
    code: PublicationFailureCode | None = None
    if run.error_code:
        try:
            code = PublicationFailureCode(run.error_code)
        except ValueError:
            code = PublicationFailureCode.PROVIDER_REJECTED
    return PublicationActivityResult(
        publication_run_id=run.id,
        status=PublicationStatus(run.status),
        phase=PublicationPhase(run.current_phase),
        video_id=run.video_id,
        confirmed_offset=int(upload.confirmed_offset) if upload else 0,
        total_bytes=int(upload.total_bytes) if upload else 0,
        processing_state=ProcessingState(run.processing_state) if run.processing_state else None,
        failure_code=code,
        retryable=code in RETRYABLE_FAILURE_CODES if code else False,
    )


def build_publication_handler(session_factory: sessionmaker[Session], blob_store: BlobStore):
    """Return the handler the activity module calls, bound to this worker."""

    def handle(step: str, request: PublicationActivityInput) -> PublicationActivityResult:
        options = _options()
        with session_factory() as session:
            pipeline = build_pipeline(session, blob_store, options)
            run = session.get(PublicationRun, request.publication_run_id)
            if run is None:
                raise RuntimeError("the publication run named by this activity no longer exists")
            try:
                asyncio.run(pipeline.run_step(step, run))
            except PublicationEligibilityError:
                # Already recorded on the run by the pipeline; the workflow reads
                # the status rather than an exception type.
                session.commit()
            except (PublicationError, OAuthFlowError) as error:
                logger.info("publication step %s stopped: %s", step, error)
                session.commit()
            session.refresh(run)
            return _projection(run, session)

    return handle
