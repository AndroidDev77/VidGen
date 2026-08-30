"""The restartable YouTube publication pipeline.

One class drives a publication from a draft to a private, processed, captioned,
thumbnailed video, and - only on an explicit later request - to its final
visibility. Every phase checkpoints, so any of them may be interrupted and
resumed by a different worker.

The ordering is not arbitrary. Captions and the thumbnail come *after* the video
exists and *after* processing, so their failure can never cause a second upload;
the visibility transition comes last and re-checks the render lineage and the
T22 gate immediately before it runs, so a render selected during a long upload
cannot be published under an older render's approval.

Every YouTube call is wrapped in the existing T23 provider instrumentation.
Quota units are recorded as a typed usage quantity with a zero monetary cost:
Google does not bill for them, and inventing a dollar figure would corrupt the
cost ledger.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar
from uuid import UUID

from opentelemetry import trace
from sqlalchemy.orm import Session

from services.publisher import youtube as capabilities
from services.publisher.contracts import (
    ChunkSource,
    ProcessingSnapshot,
    ProviderCall,
    YouTubeProvider,
    YouTubeProviderError,
)
from services.publisher.credentials import Keyring, SecretValue
from services.publisher.eligibility import (
    EligibleRender,
    PublicationEligibilityError,
    PublicationEligibilityService,
)
from services.publisher.metadata import (
    initial_draft,
    metadata_hash,
    publication_identity,
    to_provider_metadata,
    validate,
    validate_schedule,
)
from services.publisher.oauth import OAuthFlowError, YouTubeOAuthService
from services.publisher.processing import ProcessingPoller
from services.publisher.projections import result_projection
from services.publisher.resumable import ResumableUploader, chunk_source_for, release
from vidgen.contracts.publication import (
    PrivacyState,
    ProcessingState,
    PublicationAssetKind,
    PublicationAssetStatus,
    PublicationFailure,
    PublicationFailureCode,
    PublicationMetadata,
    PublicationPhase,
    PublicationResult,
    PublicationStatus,
)
from vidgen.db.models import Asset
from vidgen.db.publication_models import PublicationRun, YouTubeConnection
from vidgen.db.publication_repository import PublicationRepository, PublicationStateError
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: YouTube quota units are a rate limit, not a charge. Recorded with this unit
#: and a zero monetary cost so the T23 ledger stays truthful.
QUOTA_UNIT = capabilities.QUOTA_USAGE_UNIT
ZERO_COST = Decimal("0")


class PublicationError(RuntimeError):
    """A publication failure carrying its structured classification."""

    def __init__(self, failure: PublicationFailure) -> None:
        super().__init__(failure.summary)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class PublicationOptions:
    """Everything a caller may tune without touching the capability profile."""

    chunk_bytes: int = capabilities.DEFAULT_CHUNK_BYTES
    #: Maximum processing polls in one drive. ``None`` uses the elapsed budget.
    max_processing_polls: int | None = None
    #: Maximum chunks one upload drive sends before returning. ``None`` uploads
    #: to completion. The Temporal workflow re-enters the activity, so bounding
    #: this bounds activity duration without slowing the upload down.
    max_chunks_per_drive: int | None = None
    #: When true a caption failure holds the publication for review instead of
    #: continuing to ``PRIVATE_READY``. The default follows the documented
    #: optional-caption policy: the private video is preserved and the failure
    #: is surfaced, and the user decides.
    require_captions: bool = False
    require_thumbnail: bool = False
    trace_context: dict[str, str] = field(default_factory=dict)


class PublicationPipeline:
    """Publishes one approved, current, T22-passing render to one channel."""

    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: YouTubeProvider,
        *,
        keyring: Keyring,
        oauth: YouTubeOAuthService,
        options: PublicationOptions | None = None,
        metrics: Metrics | None = None,
        poller: ProcessingPoller | None = None,
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.options = options or PublicationOptions()
        self.metrics = metrics or Metrics()
        self.repository = PublicationRepository(session, keyring)
        self.eligibility = PublicationEligibilityService(session, blob_store)
        self.oauth = oauth
        self.poller = poller or ProcessingPoller(provider)
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.publisher")

    # -- draft ---------------------------------------------------------------
    def create_draft(
        self,
        *,
        project_id: UUID,
        owner_subject: str,
        connection_id: UUID,
        idempotency_key: str,
        thumbnail_asset_id: UUID | None = None,
        metadata: PublicationMetadata | None = None,
    ) -> PublicationRun:
        """Create, or return, the publication for this exact identity.

        The draft is built deterministically from project metadata the user
        already wrote. A second call with the same identity returns the existing
        row untouched, so a page reload or a repeated request never overwrites
        an edited draft.
        """
        gate, render = self.eligibility.evaluate(
            project_id=project_id,
            owner_subject=owner_subject,
            connection_id=connection_id,
            thumbnail_asset_id=thumbnail_asset_id,
        )
        if render is None:
            raise PublicationEligibilityError(gate)
        draft = metadata or initial_draft(render)
        validate(draft)
        identity = self._identity(render, draft)
        existing = self.repository.by_identity(identity)
        if existing is not None:
            return existing
        conflicting = self.repository.by_idempotency(project_id, idempotency_key)
        if conflicting is not None and conflicting.publication_identity != identity:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.INVALID_METADATA,
                    summary=(
                        "This idempotency key is already bound to a different publication. "
                        "Changing the selected render or channel needs a new key."
                    ),
                    reference_id=conflicting.id,
                )
            )
        run = PublicationRun(
            project_id=render.project.id,
            final_render_asset_id=render.final_asset.id,
            render_job_id=render.render_job.id,
            final_editorial_run_id=render.final_editorial_run.id,
            completion_gate_id=render.completion_gate.id,
            approval_id=render.approval.id,
            connection_id=render.connection.id,
            channel_id=render.connection.channel_id,
            owner_subject=owner_subject,
            publication_identity=identity,
            idempotency_key=idempotency_key,
            metadata_version=draft.metadata_version,
            metadata_hash=metadata_hash(draft),
            input_hash=gate.render_identity or render.render_identity,
            render_identity=render.render_identity,
            caption_asset_id=render.caption_asset.id,
            caption_asset_sha256=render.caption_asset.sha256,
            thumbnail_asset_id=render.thumbnail_asset.id if render.thumbnail_asset else None,
            thumbnail_asset_sha256=(
                render.thumbnail_asset.sha256 if render.thumbnail_asset else None
            ),
            status=PublicationStatus.DRAFT.value,
            current_phase=PublicationPhase.ELIGIBILITY.value,
            requested_privacy=draft.requested_privacy.value,
            scheduled_publish_at=draft.scheduled_publish_at,
            notify_subscribers=draft.notify_subscribers,
            contains_synthetic_media=draft.contains_synthetic_media,
            made_for_kids=draft.made_for_kids,
            draft_metadata=draft.model_dump(mode="json"),
            capability_profile_version=capabilities.CAPABILITY_PROFILE_VERSION,
            publisher_version=capabilities.PUBLISHER_VERSION,
            gate_version=render.completion_gate.gate_version,
        )
        stored, _ = self.repository.create_or_resume(run)
        return stored

    def draft_of(self, run: PublicationRun) -> PublicationMetadata:
        return PublicationMetadata.model_validate(run.draft_metadata)

    def update_draft(self, run: PublicationRun, edited: PublicationMetadata) -> PublicationRun:
        """Persist an edited draft, versioning it only when it really changed.

        After the video exists, a metadata change is applied to the *existing*
        YouTube video by :meth:`apply_metadata`; it never creates a second one.
        """
        if run.status in {
            PublicationStatus.CANCELLED.value,
            PublicationStatus.FAILED.value,
        }:
            raise PublicationStateError("a finished publication's draft can no longer be edited")
        validate(edited)
        current = self.draft_of(run)
        if metadata_hash(current) == metadata_hash(edited):
            return run
        versioned = edited.model_copy(update={"metadata_version": run.metadata_version + 1})
        run.draft_metadata = versioned.model_dump(mode="json")
        run.metadata_version = versioned.metadata_version
        run.metadata_hash = metadata_hash(versioned)
        run.requested_privacy = versioned.requested_privacy.value
        run.scheduled_publish_at = versioned.scheduled_publish_at
        run.notify_subscribers = versioned.notify_subscribers
        run.contains_synthetic_media = versioned.contains_synthetic_media
        run.made_for_kids = versioned.made_for_kids
        run.draft_edited_at = datetime.now(UTC)
        self.session.flush()
        return run

    # -- execution -----------------------------------------------------------
    async def start(self, run: PublicationRun) -> PublicationResult:
        """Drive this publication as far as ``PRIVATE_READY``.

        Idempotent by construction: every phase checks what is already durably
        true before doing anything, so calling this on a finished publication is
        a read.
        """
        return await self._drive(run)

    async def resume(self, run: PublicationRun) -> PublicationResult:
        """Continue an interrupted publication from its persisted checkpoint."""
        if run.status == PublicationStatus.HUMAN_REVIEW_REQUIRED.value:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.AMBIGUOUS_COMPLETION,
                    summary=(
                        "This publication is held for review because YouTube's outcome could "
                        "not be established. Resolve it before resuming."
                    ),
                    reference_id=run.id,
                    remediation="Check the channel's uploads, then cancel or resolve this run.",
                )
            )
        return await self._drive(run)

    async def cancel(self, run: PublicationRun) -> PublicationResult:
        """Cancel before completion, releasing the resumable session if any."""
        if run.video_id:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.PROVIDER_REJECTED,
                    summary=(
                        "The video already exists on YouTube and cannot be cancelled here. "
                        "It remains private; delete it from YouTube Studio if unwanted."
                    ),
                    reference_id=run.id,
                )
            )
        session_row = self.repository.active_session(run.id)
        if session_row is not None:
            connection = self.session.get(YouTubeConnection, run.connection_id)
            if connection is not None:
                try:
                    token = await self.oauth.access_token_for(connection)
                    await self.provider.cancel_resumable_upload(
                        access_token=token, upload_uri=self.repository.session_uri(session_row)
                    )
                except (YouTubeProviderError, OAuthFlowError, PublicationStateError):
                    # Cancellation is best effort: an unreachable session simply
                    # expires. What matters is that this run stops.
                    logger.info("resumable session could not be cancelled remotely")
            self.repository.mark_session(session_row, "cancelled")
        self.repository.transition(
            run, PublicationStatus.CANCELLED, phase=PublicationPhase.FINALIZATION
        )
        self.session.commit()
        return self.project(run)

    #: States a drive is a pure read from: the work is finished, or only an
    #: explicit user action can move it. Re-driving one of these must change
    #: nothing at all, which is what makes "retry every command" free.
    _SETTLED_STATUSES = frozenset(
        {
            PublicationStatus.PUBLISHED,
            PublicationStatus.VISIBILITY_UPDATING,
            PublicationStatus.PROCESSING_FAILED,
            PublicationStatus.CANCELLED,
            PublicationStatus.FAILED,
        }
    )

    def _finished(self, run: PublicationRun) -> bool:
        """Whether this publication has nothing left for a drive to do."""
        status = PublicationStatus(run.status)
        if status in self._SETTLED_STATUSES:
            return True
        if status is not PublicationStatus.PRIVATE_READY:
            return False
        # A private video is finished unless a caption or thumbnail is still
        # outstanding, in which case retrying those is exactly the point.
        pending = PublicationAssetStatus.FAILED.value
        for kind in (PublicationAssetKind.CAPTION, PublicationAssetKind.THUMBNAIL):
            asset = self.repository.asset(run.id, kind)
            if asset is not None and asset.status == pending:
                return False
        return True

    async def _drive(self, run: PublicationRun) -> PublicationResult:
        if self._finished(run):
            return self.project(run)
        source: ChunkSource | None = None
        try:
            render = self._revalidate(run)
            connection = render.connection
            await self.oauth.verify_channel(connection)
            self._mark_ready(run)
            self.session.commit()

            draft = self.draft_of(run)
            source = chunk_source_for(
                self.blob_store,
                key=render.final_asset.storage_key,
                byte_size=render.final_asset.byte_size,
                media_type=render.final_asset.media_type,
            )
            await self._upload(run, connection, render, draft, source)
            if self._halted(run):
                return self.project(run)

            await self._await_processing(run, connection)
            if self._halted(run) or run.processing_state != ProcessingState.SUCCEEDED.value:
                return self.project(run)

            # Each phase is checked before the next one runs. A hold one of
            # them set - a caption failure under a required-captions policy, an
            # exhausted quota, a revoked grant - must stop the drive, not be
            # walked forward into PRIVATE_READY by the next transition.
            await self._upload_captions(run, connection, render, draft)
            if self._halted(run):
                return self.project(run)
            await self._upload_thumbnail(run, connection, render)
            if self._halted(run):
                return self.project(run)
            await self._verify_private(run, connection)
            return self.project(run)
        except PublicationEligibilityError:
            self.session.commit()
            raise
        except OAuthFlowError as error:
            target = (
                PublicationStatus.REAUTHORIZATION_REQUIRED
                if error.code
                in {PublicationFailureCode.INVALID_GRANT, PublicationFailureCode.CHANNEL_MISMATCH}
                else PublicationStatus.AUTHORIZATION_REQUIRED
            )
            self._fail(run, error.code, str(error), status=target)
            raise PublicationError(
                PublicationFailure(code=error.code, summary=str(error), reference_id=run.id)
            ) from error
        finally:
            if source is not None:
                release(source)

    #: A drive stops here. Only a human decision, a quota reset or a
    #: reconnection moves a publication out of one of these.
    _HALTED_STATUSES = frozenset(
        {
            PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationStatus.QUOTA_BLOCKED,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.PROCESSING_FAILED,
            PublicationStatus.FAILED,
            PublicationStatus.CANCELLED,
        }
    )

    def _halted(self, run: PublicationRun) -> bool:
        return PublicationStatus(run.status) in self._HALTED_STATUSES

    #: States from which a drive may (re)assert readiness. Once a video exists
    #: the publication has moved past readiness and must never be walked back.
    _PRE_UPLOAD_STATUSES = frozenset(
        {
            PublicationStatus.DRAFT,
            PublicationStatus.AUTHORIZATION_REQUIRED,
            PublicationStatus.READY,
            PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationStatus.QUOTA_BLOCKED,
        }
    )

    def _mark_ready(self, run: PublicationRun) -> None:
        """Move to ``READY`` only while the publication is still pre-upload."""
        if run.video_id:
            return
        if PublicationStatus(run.status) not in self._PRE_UPLOAD_STATUSES:
            return
        self.repository.transition(
            run, PublicationStatus.READY, phase=PublicationPhase.AUTHORIZATION
        )

    #: The activity steps the Temporal workflow drives, in order. Named here so
    #: the workflow, the worker and the pipeline cannot disagree about what a
    #: step means.
    STEPS = (
        "validate_eligibility",
        "refresh_connection",
        "initialize_upload",
        "upload_chunks",
        "poll_processing",
        "upload_captions",
        "upload_thumbnail",
        "verify_private",
        "apply_visibility",
        "finalize",
    )

    async def run_step(self, step: str, run: PublicationRun) -> PublicationRun:
        """Execute exactly one workflow step against a persisted publication.

        Each step is independently restartable and reads its starting point from
        the database, so the workflow can call ``upload_chunks`` repeatedly and
        every call resumes from the server-confirmed offset.
        """
        if step not in self.STEPS:
            raise PublicationStateError(f"unknown publication step {step!r}")
        if step == "finalize":
            self.session.commit()
            return run
        render = self._revalidate(run)
        if step == "validate_eligibility":
            self._mark_ready(run)
            self.session.commit()
            return run
        connection = render.connection
        if step == "refresh_connection":
            await self.oauth.verify_channel(connection)
            self.session.commit()
            return run
        draft = self.draft_of(run)
        if step in {"initialize_upload", "upload_chunks"}:
            source = chunk_source_for(
                self.blob_store,
                key=render.final_asset.storage_key,
                byte_size=render.final_asset.byte_size,
                media_type=render.final_asset.media_type,
            )
            try:
                await self._upload(run, connection, render, draft, source)
            finally:
                release(source)
            return run
        if step == "poll_processing":
            await self._await_processing(run, connection)
            return run
        if step == "upload_captions":
            await self._upload_captions(run, connection, render, draft)
            return run
        if step == "upload_thumbnail":
            await self._upload_thumbnail(run, connection, render)
            return run
        if step == "verify_private":
            await self._verify_private(run, connection)
            return run
        # apply_visibility is only reached when a user has already recorded an
        # explicit decision; the workflow never invents one.
        if run.visibility_decision_at is None:
            return run
        await self.apply_visibility(
            run,
            privacy=PrivacyState(run.requested_privacy),
            actor=run.visibility_decided_by or run.owner_subject,
            scheduled_publish_at=run.scheduled_publish_at,
            notify_subscribers=bool(run.notify_subscribers),
        )
        return run

    # -- phases --------------------------------------------------------------
    def _revalidate(self, run: PublicationRun) -> EligibleRender:
        """Re-prove eligibility against the *current* project state.

        Called at the start of every drive and again before a visibility change:
        a render selected while an upload was running must never be published
        under the previous render's approval and gate.
        """
        gate, render = self.eligibility.evaluate(
            project_id=run.project_id,
            owner_subject=run.owner_subject,
            connection_id=run.connection_id,
            thumbnail_asset_id=run.thumbnail_asset_id,
        )
        if render is None:
            self._fail(
                run,
                gate.failures[0].code,
                gate.failures[0].summary,
                status=PublicationStatus.FAILED
                if run.video_id is None
                else PublicationStatus.HUMAN_REVIEW_REQUIRED,
            )
            raise PublicationEligibilityError(gate)
        if render.final_asset.id != run.final_render_asset_id:
            failure = PublicationFailure(
                code=PublicationFailureCode.STALE_RENDER,
                summary=(
                    "A newer render has been selected since this publication was created. "
                    "Publishing the previous render would put different media on the channel."
                ),
                reference_id=render.final_asset.id,
                remediation="Create a new publication for the current render.",
            )
            self._fail(
                run,
                failure.code,
                failure.summary,
                status=PublicationStatus.FAILED
                if run.video_id is None
                else PublicationStatus.HUMAN_REVIEW_REQUIRED,
            )
            raise PublicationEligibilityError(
                type(gate)(
                    project_id=run.project_id,
                    allowed=False,
                    failures=[failure],
                    evaluated_at=gate.evaluated_at,
                )
            )
        return render

    async def _upload(
        self,
        run: PublicationRun,
        connection: YouTubeConnection,
        render: EligibleRender,
        draft: PublicationMetadata,
        source: ChunkSource,
    ) -> None:
        if run.video_id:
            return
        token = await self.oauth.access_token_for(connection)
        session_row = self.repository.active_session(run.id)
        if session_row is None:
            self.repository.transition(
                run,
                PublicationStatus.UPLOAD_INITIALIZING,
                phase=PublicationPhase.UPLOAD_INITIALIZATION,
            )
            self.session.commit()
            # Always private, never notifying subscribers. That is not a
            # default a caller can override: the initial upload is fixed.
            video_metadata = to_provider_metadata(draft, privacy=PrivacyState.PRIVATE)
            try:
                created = await self._instrumented(
                    run,
                    "videos.insert",
                    lambda: self.provider.initialize_resumable_upload(
                        access_token=token,
                        metadata=video_metadata,
                        total_bytes=render.final_asset.byte_size,
                        media_type=render.final_asset.media_type,
                    ),
                    lambda result: result.call,
                )
            except YouTubeProviderError as error:
                self._classify(run, error)
                return
            # Persisted before a single media byte leaves this process.
            session_row = self.repository.persist_session(
                publication_run_id=run.id,
                upload_uri=created.upload_uri,
                total_bytes=render.final_asset.byte_size,
                chunk_bytes=self.options.chunk_bytes,
                expires_at=created.expires_at,
            )
            self.repository.upsert_asset(
                publication_run_id=run.id,
                kind=PublicationAssetKind.VIDEO,
                status=PublicationAssetStatus.IN_PROGRESS,
                local_asset_id=render.final_asset.id,
                local_asset_sha256=render.final_asset.sha256,
                byte_size=render.final_asset.byte_size,
            )
            self.session.commit()

        self.repository.transition(
            run, PublicationStatus.UPLOADING, phase=PublicationPhase.MEDIA_UPLOAD
        )
        self.session.commit()

        def checkpoint(offset: int, code: int | None) -> None:
            self.repository.confirm_offset(session_row, offset, response_code=code)
            self.session.commit()

        uploader = ResumableUploader(
            self.provider,
            chunk_bytes=session_row.chunk_bytes,
            on_confirmed=checkpoint,
            max_chunks_per_drive=self.options.max_chunks_per_drive,
        )
        try:
            outcome = await uploader.drive(
                access_token=token,
                upload_uri=self.repository.session_uri(session_row),
                source=source,
                total_bytes=session_row.total_bytes,
                start_offset=session_row.confirmed_offset,
                already_completed_video_id=session_row.video_id,
            )
        except YouTubeProviderError as error:
            self._classify(run, error)
            return
        self.repository.add_quota_units(run, outcome.quota_units)

        if outcome.completed and outcome.video_id:
            # The video ID is persisted before anything else happens to it.
            self.repository.record_video_id(run, outcome.video_id)
            self.repository.complete_session(session_row, outcome.video_id)
            self.repository.upsert_asset(
                publication_run_id=run.id,
                kind=PublicationAssetKind.VIDEO,
                status=PublicationAssetStatus.SUCCEEDED,
                local_asset_id=render.final_asset.id,
                local_asset_sha256=render.final_asset.sha256,
                provider_resource_id=outcome.video_id,
                byte_size=session_row.total_bytes,
                projection={"confirmed_offset": session_row.total_bytes},
            )
            self.repository.transition(
                run, PublicationStatus.PROCESSING, phase=PublicationPhase.PROCESSING_POLL
            )
            self.session.commit()
            return

        if outcome.ambiguous:
            # YouTube may have created a video. Never upload again on a guess.
            self.repository.mark_session(session_row, "ambiguous")
            self._fail(
                run,
                PublicationFailureCode.AMBIGUOUS_COMPLETION,
                "YouTube's response to the final chunk was lost and the upload session is "
                "gone, so it cannot be proven whether a video was created.",
                status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
                review_reason=(
                    f"resumable session {session_row.session_uri_hash[:16]} confirmed "
                    f"{session_row.confirmed_offset} of {session_row.total_bytes} bytes"
                ),
            )
            return
        if outcome.expired:
            # The session is gone. It is only safe to start another when the
            # server had confirmed nothing: then no video can exist.
            self.repository.mark_session(session_row, "expired")
            if session_row.confirmed_offset == 0:
                # The server confirmed nothing, so no video can exist. Replacing
                # the session automatically is provably safe here and only here.
                self.repository.transition(
                    run,
                    PublicationStatus.UPLOAD_INITIALIZING,
                    phase=PublicationPhase.UPLOAD_INITIALIZATION,
                    error_code=PublicationFailureCode.EXPIRED_RESUMABLE_SESSION.value,
                    error_summary="The upload session expired before any byte was accepted.",
                )
                self.session.commit()
                return
            self._fail(
                run,
                PublicationFailureCode.EXPIRED_RESUMABLE_SESSION,
                "The resumable upload session expired after YouTube had accepted part of the "
                "video, so it cannot be proven whether a video was created.",
                status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
                review_reason=(
                    f"expired session {session_row.session_uri_hash[:16]} at "
                    f"{session_row.confirmed_offset} of {session_row.total_bytes} bytes"
                ),
            )
            return
        # Still uploading: the caller resumes from the confirmed offset.
        self.session.commit()

    async def _await_processing(self, run: PublicationRun, connection: YouTubeConnection) -> None:
        if not run.video_id:
            return
        if run.processing_state == ProcessingState.SUCCEEDED.value:
            return
        token = await self.oauth.access_token_for(connection)

        def observe(snapshot: ProcessingSnapshot, state: ProcessingState) -> None:
            run.processing_state = state.value
            self.session.flush()

        poller = ProcessingPoller(
            self.provider,
            initial_seconds=self.poller.initial_seconds,
            max_seconds=self.poller.max_seconds,
            backoff_factor=self.poller.backoff_factor,
            max_elapsed_seconds=self.poller.max_elapsed_seconds,
            sleep=self.poller.sleep,
            on_snapshot=observe,
        )
        try:
            outcome = await poller.poll(
                access_token=token,
                video_id=run.video_id,
                max_polls=self.options.max_processing_polls,
            )
        except YouTubeProviderError as error:
            self._classify(run, error)
            return
        self.repository.add_quota_units(run, outcome.quota_units)
        run.processing_state = outcome.state.value
        if outcome.state is ProcessingState.SUCCEEDED:
            self.repository.transition(
                run, PublicationStatus.UPLOADING_CAPTIONS, phase=PublicationPhase.CAPTIONS
            )
            self.session.commit()
            return
        if outcome.state in {ProcessingState.FAILED, ProcessingState.REJECTED}:
            # The video ID and its link are deliberately retained: a processing
            # failure is investigated on YouTube, not forgotten here.
            self._fail(
                run,
                PublicationFailureCode.PROCESSING_FAILED,
                "YouTube could not process the uploaded video"
                + (
                    f" ({outcome.snapshot.failure_reason})"
                    if outcome.snapshot and outcome.snapshot.failure_reason
                    else ""
                ),
                status=PublicationStatus.PROCESSING_FAILED,
            )
            return
        # Still processing: not a failure, and never a reason to upload again.
        self.session.commit()

    async def _upload_captions(
        self,
        run: PublicationRun,
        connection: YouTubeConnection,
        render: EligibleRender,
        draft: PublicationMetadata,
    ) -> None:
        if not run.video_id:
            return
        existing = self.repository.succeeded_caption(
            run.id, draft.caption_language, draft.caption_track_name
        )
        if existing is not None:
            await self._advance_to_thumbnail(run)
            return
        caption = render.caption_asset
        if caption.sha256 != (run.caption_asset_sha256 or caption.sha256):
            self._fail(
                run,
                PublicationFailureCode.MISSING_CAPTION_ASSET,
                "The caption asset changed after this publication was created.",
                status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
            )
            return
        content = self.blob_store.read(caption.storage_key)
        if not _looks_like_srt(content):
            summary = "The canonical caption asset is not a parseable SRT track."
            self._record_caption_failure(
                run,
                PublicationFailureCode.MISSING_CAPTION_ASSET,
                summary,
                draft,
            )
            if self.options.require_captions:
                self._fail(
                    run,
                    PublicationFailureCode.MISSING_CAPTION_ASSET,
                    summary,
                    status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
                )
                return
            await self._advance_to_thumbnail(run)
            return
        token = await self.oauth.access_token_for(connection)
        try:
            track = await self._instrumented(
                run,
                "captions.insert",
                lambda: self.provider.insert_caption(
                    access_token=token,
                    video_id=run.video_id or "",
                    language=draft.caption_language,
                    name=draft.caption_track_name,
                    content=content,
                    media_type=capabilities.CANONICAL_CAPTION_MEDIA_TYPE,
                ),
                lambda result: result.call,
            )
        except YouTubeProviderError as error:
            if error.code is PublicationFailureCode.CAPTION_CONFLICT:
                # A track with this language and name already exists. Adopt it
                # rather than uploading a duplicate, and never touch a track
                # this publication did not create.
                adopted = await self._adopt_existing_caption(run, token, draft)
                if adopted is not None:
                    await self._advance_to_thumbnail(run)
                    return
            self._record_caption_failure(run, error.code, str(error), draft)
            if self.options.require_captions:
                self._fail(
                    run,
                    error.code,
                    str(error),
                    status=PublicationStatus.HUMAN_REVIEW_REQUIRED,
                )
                return
            await self._advance_to_thumbnail(run)
            return
        self.repository.upsert_asset(
            publication_run_id=run.id,
            kind=PublicationAssetKind.CAPTION,
            status=PublicationAssetStatus.SUCCEEDED,
            local_asset_id=caption.id,
            local_asset_sha256=caption.sha256,
            provider_resource_id=track.caption_id,
            language=track.language,
            name=track.name,
            byte_size=len(content),
            projection={"track_kind": track.track_kind, "is_draft": track.is_draft},
        )
        await self._advance_to_thumbnail(run)

    async def _adopt_existing_caption(
        self, run: PublicationRun, token: SecretValue, draft: PublicationMetadata
    ) -> str | None:
        try:
            tracks = await self._instrumented(
                run,
                "captions.list",
                lambda: self.provider.list_captions(
                    access_token=token, video_id=run.video_id or ""
                ),
                lambda result: result[0].call if result else ProviderCall("captions.list"),
            )
        except YouTubeProviderError:
            return None
        for track in tracks:
            if track.language == draft.caption_language and track.name == draft.caption_track_name:
                self.repository.upsert_asset(
                    publication_run_id=run.id,
                    kind=PublicationAssetKind.CAPTION,
                    status=PublicationAssetStatus.SUCCEEDED,
                    local_asset_id=run.caption_asset_id,
                    local_asset_sha256=run.caption_asset_sha256,
                    provider_resource_id=track.caption_id,
                    language=track.language,
                    name=track.name,
                    projection={"adopted_existing_track": True},
                )
                self.session.commit()
                return track.caption_id
        return None

    def _record_caption_failure(
        self,
        run: PublicationRun,
        code: PublicationFailureCode,
        summary: str,
        draft: PublicationMetadata,
    ) -> None:
        self.repository.upsert_asset(
            publication_run_id=run.id,
            kind=PublicationAssetKind.CAPTION,
            status=PublicationAssetStatus.FAILED,
            local_asset_id=run.caption_asset_id,
            local_asset_sha256=run.caption_asset_sha256,
            language=draft.caption_language,
            name=draft.caption_track_name,
            error_code=code.value,
            error_summary=summary,
        )
        self.session.commit()

    async def _advance_to_thumbnail(self, run: PublicationRun) -> None:
        if run.status != PublicationStatus.UPLOADING_THUMBNAIL.value:
            self.repository.transition(
                run, PublicationStatus.UPLOADING_THUMBNAIL, phase=PublicationPhase.THUMBNAIL
            )
        self.session.commit()

    async def _upload_thumbnail(
        self, run: PublicationRun, connection: YouTubeConnection, render: EligibleRender
    ) -> None:
        if not run.video_id:
            return
        thumbnail = render.thumbnail_asset
        existing = self.repository.asset(run.id, PublicationAssetKind.THUMBNAIL)
        if existing is not None and existing.status == PublicationAssetStatus.SUCCEEDED.value:
            return
        if thumbnail is None:
            self.repository.upsert_asset(
                publication_run_id=run.id,
                kind=PublicationAssetKind.THUMBNAIL,
                status=PublicationAssetStatus.SKIPPED,
            )
            self.session.commit()
            return
        content = self.blob_store.read(thumbnail.storage_key)
        problem = _validate_thumbnail_bytes(content, thumbnail)
        if problem is not None:
            self.repository.upsert_asset(
                publication_run_id=run.id,
                kind=PublicationAssetKind.THUMBNAIL,
                status=PublicationAssetStatus.FAILED,
                local_asset_id=thumbnail.id,
                local_asset_sha256=thumbnail.sha256,
                byte_size=len(content),
                error_code=PublicationFailureCode.INVALID_THUMBNAIL_ASSET.value,
                error_summary=problem,
            )
            self.session.commit()
            return
        token = await self.oauth.access_token_for(connection)
        try:
            result = await self._instrumented(
                run,
                "thumbnails.set",
                lambda: self.provider.set_thumbnail(
                    access_token=token,
                    video_id=run.video_id or "",
                    content=content,
                    media_type=thumbnail.media_type,
                ),
                lambda result: result.call,
            )
        except YouTubeProviderError as error:
            # A channel that cannot set custom thumbnails keeps its private
            # video; the failure is actionable, not fatal.
            self.repository.upsert_asset(
                publication_run_id=run.id,
                kind=PublicationAssetKind.THUMBNAIL,
                status=PublicationAssetStatus.FAILED,
                local_asset_id=thumbnail.id,
                local_asset_sha256=thumbnail.sha256,
                byte_size=len(content),
                error_code=error.code.value,
                error_summary=str(error),
            )
            self.session.commit()
            if self.options.require_thumbnail:
                self._fail(
                    run, error.code, str(error), status=PublicationStatus.HUMAN_REVIEW_REQUIRED
                )
            return
        self.repository.upsert_asset(
            publication_run_id=run.id,
            kind=PublicationAssetKind.THUMBNAIL,
            status=PublicationAssetStatus.SUCCEEDED,
            local_asset_id=thumbnail.id,
            local_asset_sha256=thumbnail.sha256,
            byte_size=len(content),
            projection={"width": result.width or 0, "height": result.height or 0},
        )
        self.session.commit()

    async def _verify_private(self, run: PublicationRun, connection: YouTubeConnection) -> None:
        """Confirm with YouTube that the video really is private, then stop."""
        if not run.video_id:
            return
        token = await self.oauth.access_token_for(connection)
        try:
            snapshot = await self._instrumented(
                run,
                "videos.list",
                lambda: self.provider.fetch_video(access_token=token, video_id=run.video_id or ""),
                lambda result: result.call,
            )
        except YouTubeProviderError as error:
            self._classify(run, error)
            return
        run.actual_privacy = snapshot.privacy_status or PrivacyState.PRIVATE.value
        if snapshot.contains_synthetic_media is not None:
            # Persist what YouTube says it stored, not what we asked for.
            run.contains_synthetic_media = bool(snapshot.contains_synthetic_media)
        self.repository.transition(
            run, PublicationStatus.PRIVATE_READY, phase=PublicationPhase.VERIFICATION
        )
        self.session.commit()

    # -- explicit visibility -------------------------------------------------
    async def apply_visibility(
        self,
        run: PublicationRun,
        *,
        privacy: PrivacyState,
        actor: str,
        scheduled_publish_at: datetime | None = None,
        notify_subscribers: bool = False,
        now: datetime | None = None,
    ) -> PublicationResult:
        """Change the video's visibility, only on an explicit user action.

        Everything is re-checked first: the render is still current, the T22
        gate still passes, processing succeeded, and the requested schedule is a
        valid future UTC instant. The privacy YouTube *returns* is what gets
        persisted, so a project restricted to private uploads can never be
        reported as public.
        """
        moment = now or datetime.now(UTC)
        if not run.video_id:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.PROVIDER_REJECTED,
                    summary="This publication has no uploaded video to make visible.",
                    reference_id=run.id,
                )
            )
        if run.processing_state != ProcessingState.SUCCEEDED.value:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.PROCESSING_FAILED,
                    summary="YouTube has not finished processing this video.",
                    reference_id=run.id,
                    remediation="Wait for processing to succeed, then change visibility.",
                )
            )
        # The lineage check happens again here, immediately before the change:
        # this is the last point at which a stale render could reach the public.
        render = self._revalidate(run)
        draft = self.draft_of(run)
        if scheduled_publish_at is not None:
            # A scheduled publication is a public one with a start time.
            # Scheduling an unlisted or private video is not something YouTube
            # expresses, and the database refuses to record it, so this is
            # refused here with a readable error rather than as an
            # IntegrityError on the next flush.
            if privacy is not PrivacyState.PUBLIC:
                raise PublicationError(
                    PublicationFailure(
                        code=PublicationFailureCode.INVALID_SCHEDULE,
                        summary=(
                            "A scheduled publication must request the public privacy state; "
                            f"{privacy.value} cannot be scheduled."
                        ),
                        reference_id=run.id,
                        remediation=(
                            "Choose public with a scheduled time, or remove the schedule."
                        ),
                    )
                )
            candidate = draft.model_copy(
                update={
                    "scheduled_publish_at": scheduled_publish_at,
                    "requested_privacy": PrivacyState.PUBLIC,
                }
            )
            validate_schedule(candidate, now=moment)
        connection = render.connection
        token = await self.oauth.access_token_for(connection)
        run.requested_privacy = privacy.value
        run.scheduled_publish_at = scheduled_publish_at
        run.notify_subscribers = notify_subscribers
        run.visibility_decision_at = moment
        run.visibility_decided_by = actor[:255]
        self.repository.transition(
            run, PublicationStatus.VISIBILITY_UPDATING, phase=PublicationPhase.VISIBILITY
        )
        self.session.commit()
        try:
            snapshot = await self._instrumented(
                run,
                "videos.update",
                lambda: self.provider.update_visibility(
                    access_token=token,
                    video_id=run.video_id or "",
                    # The complete status part, built from the persisted draft:
                    # the synthetic-media disclosure, the made-for-kids
                    # declaration and the embeddable setting travel with the
                    # privacy change rather than being replaced by it.
                    #
                    # A scheduled publication is submitted as a private video
                    # carrying publishAt; YouTube flips it at the given instant.
                    metadata=to_provider_metadata(
                        draft.model_copy(
                            update={
                                "scheduled_publish_at": scheduled_publish_at,
                                "notify_subscribers": notify_subscribers,
                            }
                        ),
                        privacy=(
                            PrivacyState.PRIVATE if scheduled_publish_at is not None else privacy
                        ),
                    ),
                ),
                lambda result: result.call,
            )
        except YouTubeProviderError as error:
            run.actual_privacy = run.actual_privacy or PrivacyState.PRIVATE.value
            self._classify(
                run,
                error,
                fallback_status=PublicationStatus.PRIVATE_READY,
            )
            raise PublicationError(
                PublicationFailure(
                    code=error.code,
                    summary=str(error),
                    http_status=error.http_status,
                    provider_reason=error.reason,
                    reference_id=run.id,
                    remediation=error.remediation,
                )
            ) from error
        run.actual_privacy = snapshot.privacy_status or PrivacyState.PRIVATE.value
        if snapshot.publish_at is not None:
            run.scheduled_publish_at = snapshot.publish_at
        self.repository.transition(
            run, PublicationStatus.PUBLISHED, phase=PublicationPhase.FINALIZATION
        )
        self.session.commit()
        return self.project(run)

    async def apply_metadata(self, run: PublicationRun) -> PublicationResult:
        """Push an edited draft onto the *existing* YouTube video.

        Material metadata changes create a new metadata version. They never
        create a second video: the same ``videos.update`` call that changes a
        title is the one that changes it after publication.
        """
        if not run.video_id:
            raise PublicationError(
                PublicationFailure(
                    code=PublicationFailureCode.PROVIDER_REJECTED,
                    summary="This publication has no uploaded video to update.",
                    reference_id=run.id,
                )
            )
        render = self._revalidate(run)
        draft = self.draft_of(run)
        validate(draft)
        token = await self.oauth.access_token_for(render.connection)
        current = PrivacyState(run.actual_privacy or PrivacyState.PRIVATE.value)
        snapshot = await self._instrumented(
            run,
            "videos.update",
            lambda: self.provider.update_metadata(
                access_token=token,
                video_id=run.video_id or "",
                metadata=to_provider_metadata(draft, privacy=current),
            ),
            lambda result: result.call,
        )
        if snapshot.contains_synthetic_media is not None:
            run.contains_synthetic_media = bool(snapshot.contains_synthetic_media)
        self.session.commit()
        return self.project(run)

    # -- helpers -------------------------------------------------------------
    def project(self, run: PublicationRun) -> PublicationResult:
        return result_projection(self.session, run)

    def _identity(self, render: EligibleRender, draft: PublicationMetadata) -> str:
        return publication_identity(
            project_id=render.project.id,
            final_render_asset_id=render.final_asset.id,
            final_render_sha256=render.final_asset.sha256,
            final_editorial_run_id=render.final_editorial_run.id,
            final_report_hash=render.final_editorial_run.final_qa_identity,
            approval_id=render.approval.id,
            connection_id=render.connection.id,
            channel_id=render.connection.channel_id,
            metadata_version=draft.metadata_version,
            metadata_digest=metadata_hash(draft),
            caption_asset_id=render.caption_asset.id,
            caption_sha256=render.caption_asset.sha256,
            thumbnail_asset_id=render.thumbnail_asset.id if render.thumbnail_asset else None,
            thumbnail_sha256=render.thumbnail_asset.sha256 if render.thumbnail_asset else None,
        )

    def _fail(
        self,
        run: PublicationRun,
        code: PublicationFailureCode,
        summary: str,
        *,
        status: PublicationStatus,
        review_reason: str | None = None,
    ) -> None:
        try:
            self.repository.transition(
                run,
                status,
                error_code=code.value,
                error_summary=summary,
                review_reason=review_reason,
            )
        except PublicationStateError:
            # The machine forbids this move: record the classification without
            # corrupting the state, and let the caller surface the error.
            run.error_code = code.value
            run.error_summary = summary[:500]
            self.session.flush()
        self.session.commit()

    def _classify(
        self,
        run: PublicationRun,
        error: YouTubeProviderError,
        *,
        fallback_status: PublicationStatus | None = None,
    ) -> None:
        """Map a provider failure onto the state the publication should wait in."""
        mapping = {
            PublicationFailureCode.QUOTA_EXCEEDED: PublicationStatus.QUOTA_BLOCKED,
            PublicationFailureCode.UPLOAD_LIMIT_EXCEEDED: PublicationStatus.QUOTA_BLOCKED,
            PublicationFailureCode.RATE_LIMITED: PublicationStatus.QUOTA_BLOCKED,
            PublicationFailureCode.INVALID_GRANT: PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationFailureCode.INSUFFICIENT_SCOPE: PublicationStatus.REAUTHORIZATION_REQUIRED,
            PublicationFailureCode.AUTHENTICATION_REQUIRED: (
                PublicationStatus.REAUTHORIZATION_REQUIRED
            ),
            PublicationFailureCode.AMBIGUOUS_COMPLETION: PublicationStatus.HUMAN_REVIEW_REQUIRED,
            PublicationFailureCode.EXPIRED_RESUMABLE_SESSION: (
                PublicationStatus.HUMAN_REVIEW_REQUIRED
            ),
            PublicationFailureCode.PROCESSING_FAILED: PublicationStatus.PROCESSING_FAILED,
        }
        target = mapping.get(error.code)
        if target is None:
            target = fallback_status or (
                PublicationStatus.HUMAN_REVIEW_REQUIRED
                if run.video_id
                else PublicationStatus.FAILED
            )
        self._fail(run, error.code, str(error), status=target)

    async def _instrumented(
        self,
        run: PublicationRun,
        operation: str,
        call: Callable[[], Awaitable[T]],
        call_of: Callable[[T], ProviderCall],
    ) -> T:
        """Run one YouTube operation inside the existing T23 instrumentation.

        Quota units land as a typed usage quantity with a zero monetary cost.
        No token, session URI or raw payload is ever recorded: the metadata is
        the operation, the video ID and the confirmed offset.
        """
        idempotency_key = f"{run.publication_identity[:32]}:{operation}:{run.metadata_version}"
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=run.project_id,
            provider=self.provider.name,
            model=capabilities.CAPABILITY_PROFILE_VERSION,
            operation=f"youtube.{operation}",
            input_hash=run.publication_identity,
            idempotency_key=idempotency_key,
            related_entity_id=run.id,
            estimated_cost=ZERO_COST,
        ) as attempt:
            try:
                result = await call()
            except YouTubeProviderError as error:
                attempt.set_result(
                    provider_request_id=error.provider_request_id or None,
                    usage=[{"unit": QUOTA_UNIT, "quantity": error.quota_units}],
                    actual_cost=ZERO_COST,
                    metadata={
                        "operation": operation,
                        "failure_class": error.code.value,
                        "http_status": error.http_status,
                        "video_id": run.video_id or "",
                        "quota_units": error.quota_units,
                    },
                )
                self.session.commit()
                raise
            provider_call = call_of(result)
            self.repository.add_quota_units(run, provider_call.quota_units)
            attempt.set_result(
                provider_request_id=provider_call.provider_request_id or None,
                usage=[{"unit": QUOTA_UNIT, "quantity": provider_call.quota_units}],
                actual_cost=ZERO_COST,
                metadata={
                    "operation": operation,
                    "http_status": provider_call.http_status,
                    "retry_count": provider_call.retry_count,
                    "video_id": run.video_id or "",
                    # A rate-limit quantity, recorded beside the attempt so the
                    # deployed workbook can chart it without inventing a cost.
                    "quota_units": provider_call.quota_units,
                },
            )
            self.session.commit()
            return result


def _looks_like_srt(content: bytes) -> bool:
    """A cheap structural check: a numbered cue with an SRT timing line.

    Not a full parser - the T17 caption validator already produced this file -
    but enough to refuse an empty or truncated asset before spending 400 quota
    units on it.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if "-->" not in text:
        return False
    first = text.strip().splitlines()
    return len(first) >= 2 and first[0].strip().isdigit()


def _validate_thumbnail_bytes(content: bytes, asset: Asset) -> str | None:
    """Decode and measure a thumbnail. Returns a problem, or ``None``."""
    if len(content) != asset.byte_size:
        return "The thumbnail's stored bytes no longer match its recorded size."
    if len(content) > capabilities.MAX_THUMBNAIL_BYTES:
        return "The thumbnail exceeds YouTube's 2 MiB limit."
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
    except Exception:  # any decode failure gives the same actionable answer
        return "The selected thumbnail could not be decoded as a JPEG or PNG image."
    if width < capabilities.MIN_THUMBNAIL_WIDTH:
        return (
            f"The thumbnail is {width}px wide; YouTube recommends at least "
            f"{capabilities.MIN_THUMBNAIL_WIDTH}px."
        )
    if height <= 0:
        return "The thumbnail has no height."
    return None
