"""Bounded projections of publication state for the API, CLI and dashboard.

Everything a caller sees comes from here, and nothing here can carry a
credential: there is no field for an access token, a refresh token, a resumable
session URI, an authorization code or a raw YouTube payload, and the session is
represented only by the first bytes of its URI *hash*, which is evidence rather
than access.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.publisher import youtube as capabilities
from vidgen.contracts.publication import (
    PrivacyState,
    ProcessingState,
    PublicationAssetKind,
    PublicationAssetResult,
    PublicationAssetStatus,
    PublicationAttempt,
    PublicationFailure,
    PublicationFailureCode,
    PublicationPhase,
    PublicationProgress,
    PublicationResult,
    PublicationStatus,
    ResumableUploadCheckpoint,
)
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.publication_models import PublicationAsset, PublicationRun, YouTubeUploadSession


def _failure_of(run: PublicationRun) -> PublicationFailure | None:
    if not run.error_code:
        return None
    try:
        code = PublicationFailureCode(run.error_code)
    except ValueError:
        code = PublicationFailureCode.PROVIDER_REJECTED
    return PublicationFailure(
        code=code,
        summary=(run.error_summary or code.value)[:500],
        retryable=code
        in {PublicationFailureCode.RATE_LIMITED, PublicationFailureCode.QUOTA_EXCEEDED},
        reference_id=run.id,
        remediation=(run.review_reason or "")[:500],
    )


def caption_asset(session: Session, run_id: UUID) -> PublicationAsset | None:
    return session.scalar(
        select(PublicationAsset)
        .where(
            PublicationAsset.publication_run_id == run_id,
            PublicationAsset.kind == PublicationAssetKind.CAPTION.value,
        )
        .order_by(PublicationAsset.created_at.desc())
    )


def thumbnail_asset(session: Session, run_id: UUID) -> PublicationAsset | None:
    return session.scalar(
        select(PublicationAsset).where(
            PublicationAsset.publication_run_id == run_id,
            PublicationAsset.kind == PublicationAssetKind.THUMBNAIL.value,
        )
    )


def latest_session(session: Session, run_id: UUID) -> YouTubeUploadSession | None:
    return session.scalar(
        select(YouTubeUploadSession)
        .where(YouTubeUploadSession.publication_run_id == run_id)
        .order_by(YouTubeUploadSession.created_at.desc())
    )


def asset_projection(row: PublicationAsset) -> PublicationAssetResult:
    failure = None
    if row.error_code:
        try:
            code = PublicationFailureCode(row.error_code)
        except ValueError:
            code = PublicationFailureCode.PROVIDER_REJECTED
        failure = PublicationFailure(
            code=code, summary=(row.error_summary or code.value)[:500], reference_id=row.id
        )
    return PublicationAssetResult(
        publication_asset_id=row.id,
        publication_run_id=row.publication_run_id,
        kind=PublicationAssetKind(row.kind),
        status=PublicationAssetStatus(row.status),
        local_asset_id=row.local_asset_id,
        local_asset_sha256=row.local_asset_sha256,
        provider_resource_id=row.provider_resource_id or "",
        provider_attempt_id=row.provider_attempt_id,
        byte_size=int(row.byte_size or 0),
        language=row.language or "",
        name=row.name or "",
        failure=failure,
        projection={
            key: value
            for key, value in (row.projection or {}).items()
            if isinstance(value, (str, int, bool)) or value is None
        },
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def checkpoint_projection(row: YouTubeUploadSession) -> ResumableUploadCheckpoint:
    """Project a session without its URI. The hash is evidence, not access."""
    return ResumableUploadCheckpoint(
        session_id=row.id,
        publication_run_id=row.publication_run_id,
        session_uri_hash=row.session_uri_hash,
        encryption_key_version=row.encryption_key_version,
        total_bytes=int(row.total_bytes),
        confirmed_offset=int(row.confirmed_offset),
        chunk_bytes=int(row.chunk_bytes),
        status=row.status,  # type: ignore[arg-type]
        last_response_code=row.last_response_code,
        video_id=row.video_id,
        provider_attempt_id=row.provider_attempt_id,
        expires_at=row.expires_at,
        last_confirmed_at=row.last_confirmed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def progress_projection(session: Session, run: PublicationRun) -> PublicationProgress:
    upload = latest_session(session, run.id)
    caption = caption_asset(session, run.id)
    thumbnail = thumbnail_asset(session, run.id)
    return PublicationProgress(
        publication_run_id=run.id,
        status=PublicationStatus(run.status),
        phase=PublicationPhase(run.current_phase),
        total_bytes=int(upload.total_bytes) if upload else 0,
        confirmed_offset=int(upload.confirmed_offset) if upload else 0,
        chunk_bytes=int(upload.chunk_bytes) if upload else 0,
        processing_state=ProcessingState(run.processing_state) if run.processing_state else None,
        caption_status=PublicationAssetStatus(caption.status) if caption else None,
        thumbnail_status=PublicationAssetStatus(thumbnail.status) if thumbnail else None,
        quota_units=int(run.quota_units or 0),
        updated_at=run.updated_at,
    )


def attempt_projections(session: Session, run: PublicationRun) -> list[PublicationAttempt]:
    """The T23 provider attempts for this publication, projected and bounded.

    T25 records no attempt table of its own: this reads the shared
    ``provider_attempts`` rows the instrumentation already wrote.
    """
    rows = session.scalars(
        select(ProviderAttempt)
        .where(
            ProviderAttempt.project_id == run.project_id,
            ProviderAttempt.related_entity_id == run.id,
        )
        .order_by(ProviderAttempt.started_at)
    ).all()
    attempts: list[PublicationAttempt] = []
    for index, row in enumerate(rows, start=1):
        quota = 0
        for entry in row.usage or []:
            if isinstance(entry, dict) and entry.get("unit") == capabilities.QUOTA_USAGE_UNIT:
                try:
                    quota += int(entry.get("quantity", 0))
                except (TypeError, ValueError):
                    quota += 0
        failure = None
        if row.status == "FAILED":
            try:
                code = PublicationFailureCode(row.failure_class or "")
            except ValueError:
                code = PublicationFailureCode.PROVIDER_REJECTED
            failure = PublicationFailure(
                code=code, summary=(row.error_code or code.value)[:500], reference_id=row.id
            )
        attempts.append(
            PublicationAttempt(
                attempt_id=row.id,
                publication_run_id=run.id,
                operation=row.operation[:64],
                attempt_number=index,
                provider_attempt_id=row.id,
                provider=row.provider,
                status=(
                    "succeeded"
                    if row.status == "SUCCEEDED"
                    else "failed"
                    if row.status == "FAILED"
                    else "started"
                ),
                provider_request_id=(row.provider_request_id or "")[:255],
                latency_ms=int(row.latency_ms or 0),
                quota_units=quota,
                failure=failure,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
        )
    return attempts


def result_projection(
    session: Session, run: PublicationRun, *, reused: bool = False
) -> PublicationResult:
    """The compact publication projection returned everywhere."""
    upload = latest_session(session, run.id)
    caption = caption_asset(session, run.id)
    thumbnail = thumbnail_asset(session, run.id)
    return PublicationResult(
        publication_run_id=run.id,
        project_id=run.project_id,
        connection_id=run.connection_id,
        channel_id=run.channel_id,
        final_render_asset_id=run.final_render_asset_id,
        final_editorial_run_id=run.final_editorial_run_id,
        approval_id=run.approval_id,
        publication_identity=run.publication_identity,
        idempotency_key=run.idempotency_key,
        metadata_version=int(run.metadata_version),
        status=PublicationStatus(run.status),
        phase=PublicationPhase(run.current_phase),
        video_id=run.video_id,
        video_url=capabilities.watch_url(run.video_id) if run.video_id else "",
        total_bytes=int(upload.total_bytes) if upload else 0,
        confirmed_offset=int(upload.confirmed_offset) if upload else 0,
        processing_state=ProcessingState(run.processing_state) if run.processing_state else None,
        caption_status=PublicationAssetStatus(caption.status) if caption else None,
        caption_track_id=(caption.provider_resource_id or "") if caption else "",
        thumbnail_status=PublicationAssetStatus(thumbnail.status) if thumbnail else None,
        requested_privacy=PrivacyState(run.requested_privacy),
        actual_privacy=PrivacyState(run.actual_privacy) if run.actual_privacy else None,
        scheduled_publish_at=run.scheduled_publish_at,
        contains_synthetic_media=bool(run.contains_synthetic_media),
        made_for_kids=bool(run.made_for_kids),
        notify_subscribers=bool(run.notify_subscribers),
        quota_units=int(run.quota_units or 0),
        capability_profile_version=run.capability_profile_version or "",
        publisher_version=run.publisher_version or "",
        failure=_failure_of(run),
        reused=reused,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
