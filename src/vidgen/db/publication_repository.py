"""Transactional persistence for T25 connections, publications and uploads.

Everything that must be atomic lives here: sealing a credential and marking the
connection connected, persisting a resumable session *before* any byte is sent,
advancing a confirmed offset, and moving a publication through its state machine.

The repository is the only place that touches ciphertext columns. Callers hand
it plaintext inside a :class:`~services.publisher.credentials.SecretValue` and
get one back; nothing in between ever sees a token as a bare string.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from vidgen.contracts.publication import (
    ConnectionStatus,
    PublicationAssetKind,
    PublicationAssetStatus,
    PublicationPhase,
    PublicationStatus,
    transition_allowed,
)
from vidgen.db.publication_models import (
    PublicationAsset,
    PublicationRun,
    YouTubeConnection,
    YouTubeConnectionSecret,
    YouTubeOAuthState,
    YouTubeUploadSession,
)
from vidgen.security.envelope import (
    PURPOSE_ACCESS_TOKEN,
    PURPOSE_PKCE_VERIFIER,
    PURPOSE_REFRESH_TOKEN,
    PURPOSE_SESSION_URI,
    Keyring,
    SealedSecret,
    SecretValue,
    connection_context,
    session_context,
    state_context,
)


class PublicationStateError(RuntimeError):
    """An attempt to move a publication somewhere the state machine forbids."""


def state_hash(state: str) -> str:
    """The stored fingerprint of an OAuth ``state`` parameter."""
    return hashlib.sha256(state.encode()).hexdigest()


def uri_hash(uri: str) -> str:
    """The stored fingerprint of a resumable session URI."""
    return hashlib.sha256(uri.encode()).hexdigest()


class PublicationRepository:
    def __init__(self, session: Session, keyring: Keyring) -> None:
        self.session = session
        self.keyring = keyring

    # -- connections ---------------------------------------------------------
    def connections_for_owner(self, owner_subject: str) -> Sequence[YouTubeConnection]:
        return self.session.scalars(
            select(YouTubeConnection)
            .where(
                YouTubeConnection.owner_subject == owner_subject,
                YouTubeConnection.status != ConnectionStatus.DISCONNECTED.value,
            )
            .order_by(YouTubeConnection.created_at.desc())
        ).all()

    def owned_connection(self, connection_id: UUID, owner_subject: str) -> YouTubeConnection | None:
        """A connection, only when this owner controls it.

        A foreign connection is indistinguishable from a missing one: the caller
        renders the same 404 either way, so a connection ID cannot be probed.
        """
        return self.session.scalar(
            select(YouTubeConnection).where(
                YouTubeConnection.id == connection_id,
                YouTubeConnection.owner_subject == owner_subject,
            )
        )

    def connection_by_channel(
        self, owner_subject: str, channel_id: str
    ) -> YouTubeConnection | None:
        return self.session.scalar(
            select(YouTubeConnection).where(
                YouTubeConnection.owner_subject == owner_subject,
                YouTubeConnection.channel_id == channel_id,
                YouTubeConnection.status != ConnectionStatus.DISCONNECTED.value,
            )
        )

    def upsert_connection(
        self,
        *,
        owner_subject: str,
        channel_id: str,
        channel_title: str,
        channel_thumbnail_url: str,
        custom_url: str,
        granted_scopes: Sequence[str],
        refresh_token: SecretValue | None,
        access_token: SecretValue | None,
        access_token_expires_at: datetime | None,
    ) -> YouTubeConnection:
        """Create or reconnect one channel, sealing its credentials atomically.

        A reconnect that returns no new refresh token keeps the sealed one
        already stored: Google issues a refresh token only on the first offline
        grant, and discarding the old one would silently break renewal.
        """
        now = datetime.now(UTC)
        connection = self.connection_by_channel(owner_subject, channel_id)
        if connection is None:
            # The identity is chosen before the insert so the credential can be
            # sealed against it: "connected implies a stored credential" is a
            # database constraint, so the row may never exist in a state where
            # it is connected and the ciphertext is not yet written.
            connection = YouTubeConnection(
                id=uuid4(),
                owner_subject=owner_subject,
                channel_id=channel_id,
                channel_title=channel_title,
                channel_thumbnail_url=channel_thumbnail_url,
                custom_url=custom_url,
                granted_scopes=list(granted_scopes),
                status=ConnectionStatus.CONNECTED.value,
                encryption_key_version=self.keyring.active_version,
                credential_present=True,
                credential_expires_at=access_token_expires_at,
                last_verified_at=now,
            )
            created = True
        else:
            created = False
            connection.channel_title = channel_title
            connection.channel_thumbnail_url = channel_thumbnail_url
            connection.custom_url = custom_url
            connection.granted_scopes = list(granted_scopes)
            connection.credential_expires_at = access_token_expires_at
            connection.last_verified_at = now
            connection.error_code = None
            connection.disconnected_at = None

        secret = None if created else self._secret_row(connection.id)
        context = connection_context(connection.id)
        if refresh_token is not None:
            sealed_refresh = self.keyring.seal(
                refresh_token.reveal(), purpose=PURPOSE_REFRESH_TOKEN, context=context
            )
        elif secret is not None:
            sealed_refresh = SealedSecret(
                ciphertext=secret.refresh_token_ciphertext,
                nonce=secret.refresh_token_nonce,
                key_version=secret.encryption_key_version,
                purpose=PURPOSE_REFRESH_TOKEN,
            )
        else:
            raise PublicationStateError(
                "a new YouTube connection requires an offline refresh credential; "
                "re-run the authorization with access_type=offline and prompt=consent"
            )

        sealed_access = (
            self.keyring.seal(access_token.reveal(), purpose=PURPOSE_ACCESS_TOKEN, context=context)
            if access_token is not None
            else None
        )
        if created:
            self.session.add(connection)
        if secret is None:
            secret = YouTubeConnectionSecret(
                connection_id=connection.id,
                refresh_token_ciphertext=sealed_refresh.ciphertext,
                refresh_token_nonce=sealed_refresh.nonce,
                encryption_key_version=sealed_refresh.key_version,
            )
            self.session.add(secret)
        else:
            secret.refresh_token_ciphertext = sealed_refresh.ciphertext
            secret.refresh_token_nonce = sealed_refresh.nonce
            secret.encryption_key_version = sealed_refresh.key_version
        secret.access_token_ciphertext = sealed_access.ciphertext if sealed_access else None
        secret.access_token_nonce = sealed_access.nonce if sealed_access else None
        secret.access_token_expires_at = access_token_expires_at

        connection.status = ConnectionStatus.CONNECTED.value
        connection.credential_present = True
        connection.encryption_key_version = sealed_refresh.key_version
        self.session.flush()
        return connection

    def _secret_row(self, connection_id: UUID) -> YouTubeConnectionSecret | None:
        return self.session.scalar(
            select(YouTubeConnectionSecret).where(
                YouTubeConnectionSecret.connection_id == connection_id
            )
        )

    def refresh_token_for(self, connection: YouTubeConnection) -> SecretValue:
        secret = self._secret_row(connection.id)
        if secret is None:
            raise PublicationStateError(
                "this YouTube connection has no stored credential; reconnect the channel"
            )
        return self.keyring.open(
            SealedSecret(
                ciphertext=secret.refresh_token_ciphertext,
                nonce=secret.refresh_token_nonce,
                key_version=secret.encryption_key_version,
                purpose=PURPOSE_REFRESH_TOKEN,
            ),
            context=connection_context(connection.id),
        )

    def cached_access_token(
        self, connection: YouTubeConnection
    ) -> tuple[SecretValue, datetime] | None:
        """A still-valid sealed access token, or ``None`` when one must be minted."""
        secret = self._secret_row(connection.id)
        if secret is None or secret.access_token_ciphertext is None:
            return None
        if secret.access_token_nonce is None or secret.access_token_expires_at is None:
            return None
        expires = secret.access_token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        token = self.keyring.open(
            SealedSecret(
                ciphertext=secret.access_token_ciphertext,
                nonce=secret.access_token_nonce,
                key_version=secret.encryption_key_version,
                purpose=PURPOSE_ACCESS_TOKEN,
            ),
            context=connection_context(connection.id),
        )
        return token, expires

    def store_access_token(
        self, connection: YouTubeConnection, token: SecretValue, expires_at: datetime
    ) -> None:
        secret = self._secret_row(connection.id)
        if secret is None:
            raise PublicationStateError("cannot cache an access token for a disconnected channel")
        sealed = self.keyring.seal(
            token.reveal(), purpose=PURPOSE_ACCESS_TOKEN, context=connection_context(connection.id)
        )
        secret.access_token_ciphertext = sealed.ciphertext
        secret.access_token_nonce = sealed.nonce
        secret.access_token_expires_at = expires_at
        connection.credential_expires_at = expires_at
        connection.last_verified_at = datetime.now(UTC)
        self.session.flush()

    def rotate_connection_keys(self, connection: YouTubeConnection) -> bool:
        """Re-seal a connection's credentials under the active key version.

        Returns whether anything changed, so a rotation sweep can report a count
        without decrypting rows that are already current.
        """
        secret = self._secret_row(connection.id)
        if secret is None or secret.encryption_key_version == self.keyring.active_version:
            return False
        context = connection_context(connection.id)
        resealed = self.keyring.reseal(
            SealedSecret(
                ciphertext=secret.refresh_token_ciphertext,
                nonce=secret.refresh_token_nonce,
                key_version=secret.encryption_key_version,
                purpose=PURPOSE_REFRESH_TOKEN,
            ),
            context=context,
        )
        secret.refresh_token_ciphertext = resealed.ciphertext
        secret.refresh_token_nonce = resealed.nonce
        if secret.access_token_ciphertext is not None and secret.access_token_nonce is not None:
            resealed_access = self.keyring.reseal(
                SealedSecret(
                    ciphertext=secret.access_token_ciphertext,
                    nonce=secret.access_token_nonce,
                    key_version=secret.encryption_key_version,
                    purpose=PURPOSE_ACCESS_TOKEN,
                ),
                context=context,
            )
            secret.access_token_ciphertext = resealed_access.ciphertext
            secret.access_token_nonce = resealed_access.nonce
        secret.encryption_key_version = self.keyring.active_version
        connection.encryption_key_version = self.keyring.active_version
        self.session.flush()
        return True

    def mark_reauthorization_required(self, connection: YouTubeConnection, error_code: str) -> None:
        connection.status = ConnectionStatus.REAUTHORIZATION_REQUIRED.value
        connection.error_code = error_code[:128]
        self.session.flush()

    def disconnect(self, connection: YouTubeConnection, *, revoked: bool) -> None:
        """Forget the credential and mark the connection gone.

        The ciphertext row is deleted rather than retained: a disconnected
        channel must not leave a decryptable refresh token behind.
        """
        secret = self._secret_row(connection.id)
        if secret is not None:
            self.session.delete(secret)
        connection.credential_present = False
        connection.status = (
            ConnectionStatus.REVOKED.value if revoked else ConnectionStatus.DISCONNECTED.value
        )
        connection.disconnected_at = datetime.now(UTC)
        self.session.flush()

    # -- OAuth state ---------------------------------------------------------
    def create_oauth_state(
        self,
        *,
        state: str,
        owner_subject: str,
        code_verifier: SecretValue,
        redirect_uri: str,
        redirect_target: str,
        requested_scopes: Sequence[str],
        expires_at: datetime,
    ) -> YouTubeOAuthState:
        now = datetime.now(UTC)
        row = YouTubeOAuthState(
            state_hash=state_hash(state),
            owner_subject=owner_subject,
            code_verifier_ciphertext=b"\x00",
            code_verifier_nonce=b"\x00" * 12,
            encryption_key_version=self.keyring.active_version,
            redirect_uri=redirect_uri,
            redirect_target=redirect_target,
            requested_scopes=list(requested_scopes),
            expires_at=expires_at,
            created_at=now,
        )
        self.session.add(row)
        self.session.flush()
        # Sealed after the flush so the row's own ID can be the AAD context: a
        # verifier lifted into another state row then fails authentication.
        sealed = self.keyring.seal(
            code_verifier.reveal(), purpose=PURPOSE_PKCE_VERIFIER, context=state_context(row.id)
        )
        row.code_verifier_ciphertext = sealed.ciphertext
        row.code_verifier_nonce = sealed.nonce
        row.encryption_key_version = sealed.key_version
        self.session.flush()
        return row

    def consume_oauth_state(self, state: str, *, now: datetime | None = None) -> YouTubeOAuthState:
        """Atomically claim one unconsumed, unexpired state, or fail.

        The claim is a single conditional UPDATE whose WHERE clause carries the
        whole precondition, so two callbacks racing with the same state cannot
        both proceed: exactly one UPDATE matches a row, and the other sees zero.
        A read-then-write here would let both pass the check before either
        wrote, which is precisely the replay the one-time state exists to stop.
        """
        moment = now or datetime.now(UTC)
        digest = state_hash(state)
        # ``CursorResult`` is what a DML statement really returns; only that
        # type carries the matched-row count the claim turns on.
        claimed = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(YouTubeOAuthState)
                .where(
                    YouTubeOAuthState.state_hash == digest,
                    YouTubeOAuthState.consumed_at.is_(None),
                    YouTubeOAuthState.expires_at > moment,
                )
                .values(consumed_at=moment)
            ),
        )
        if claimed.rowcount == 1:
            row = self.session.scalar(
                select(YouTubeOAuthState).where(YouTubeOAuthState.state_hash == digest)
            )
            if row is not None:
                return row
        # The UPDATE matched nothing. Read the row only to say *why*, and never
        # to decide: the decision was made by the statement above.
        existing = self.session.scalar(
            select(YouTubeOAuthState).where(YouTubeOAuthState.state_hash == digest)
        )
        if existing is None:
            raise PublicationStateError("this authorization request is unknown")
        if existing.consumed_at is not None:
            raise PublicationStateError("this authorization request has already been used")
        raise PublicationStateError("this authorization request has expired")

    def code_verifier_for(self, row: YouTubeOAuthState) -> SecretValue:
        return self.keyring.open(
            SealedSecret(
                ciphertext=row.code_verifier_ciphertext,
                nonce=row.code_verifier_nonce,
                key_version=row.encryption_key_version,
                purpose=PURPOSE_PKCE_VERIFIER,
            ),
            context=state_context(row.id),
        )

    def purge_expired_states(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        rows = self.session.scalars(
            select(YouTubeOAuthState).where(YouTubeOAuthState.expires_at <= moment)
        ).all()
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)

    # -- publication runs ----------------------------------------------------
    def by_identity(self, publication_identity: str) -> PublicationRun | None:
        return self.session.scalar(
            select(PublicationRun).where(
                PublicationRun.publication_identity == publication_identity
            )
        )

    def by_idempotency(self, project_id: UUID, idempotency_key: str) -> PublicationRun | None:
        return self.session.scalar(
            select(PublicationRun).where(
                PublicationRun.project_id == project_id,
                PublicationRun.idempotency_key == idempotency_key,
            )
        )

    def owned_run(
        self, publication_id: UUID, project_id: UUID, owner_subject: str
    ) -> PublicationRun | None:
        return self.session.scalar(
            select(PublicationRun).where(
                PublicationRun.id == publication_id,
                PublicationRun.project_id == project_id,
                PublicationRun.owner_subject == owner_subject,
            )
        )

    def runs_for_project(self, project_id: UUID) -> Sequence[PublicationRun]:
        return self.session.scalars(
            select(PublicationRun)
            .where(PublicationRun.project_id == project_id)
            .order_by(PublicationRun.created_at.desc())
        ).all()

    def create_or_resume(self, run: PublicationRun) -> tuple[PublicationRun, bool]:
        """Return the existing run for this identity, or persist the new one.

        This is what makes retrying a completed publication free: the identity
        binds the render, gate, approval, connection, metadata, caption,
        thumbnail, publisher and capability versions, so the same request always
        finds the same row.
        """
        existing = self.by_identity(run.publication_identity)
        if existing is not None:
            return existing, True
        self.session.add(run)
        self.session.flush()
        return run, False

    def transition(
        self,
        run: PublicationRun,
        target: PublicationStatus,
        *,
        phase: PublicationPhase | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        review_reason: str | None = None,
    ) -> None:
        """Move a run to ``target``, refusing anything the machine forbids.

        The same rule is a database CHECK; this raises the readable error before
        the constraint fires, so a bug is a named exception rather than an
        IntegrityError on flush.
        """
        current = PublicationStatus(run.status)
        if current is not target and not transition_allowed(current, target):
            raise PublicationStateError(
                f"a publication may not move from {current.value} to {target.value}"
            )
        if current is not target:
            run.previous_status = current.value
            run.status = target.value
        if phase is not None:
            run.current_phase = phase.value
        run.error_code = error_code[:128] if error_code else None
        run.error_summary = error_summary[:500] if error_summary else None
        if review_reason is not None:
            run.review_reason = review_reason[:500]
        if run.started_at is None and target is not PublicationStatus.DRAFT:
            run.started_at = datetime.now(UTC)
        if target in {
            PublicationStatus.PUBLISHED,
            PublicationStatus.FAILED,
            PublicationStatus.CANCELLED,
        }:
            run.completed_at = datetime.now(UTC)
        self.session.flush()

    def record_video_id(self, run: PublicationRun, video_id: str) -> None:
        """Persist the YouTube video ID. Always before any processing poll."""
        if run.video_id and run.video_id != video_id:
            raise PublicationStateError(
                "this publication already created a different YouTube video; "
                "refusing to overwrite the recorded provenance"
            )
        run.video_id = video_id
        self.session.flush()

    def add_quota_units(self, run: PublicationRun, units: int) -> None:
        if units < 0:
            raise ValueError("quota units are never negative")
        run.quota_units = int(run.quota_units) + units
        self.session.flush()

    # -- upload sessions -----------------------------------------------------
    def active_session(self, publication_run_id: UUID) -> YouTubeUploadSession | None:
        return self.session.scalar(
            select(YouTubeUploadSession).where(
                YouTubeUploadSession.publication_run_id == publication_run_id,
                YouTubeUploadSession.status == "active",
            )
        )

    def latest_session(self, publication_run_id: UUID) -> YouTubeUploadSession | None:
        return self.session.scalar(
            select(YouTubeUploadSession)
            .where(YouTubeUploadSession.publication_run_id == publication_run_id)
            .order_by(YouTubeUploadSession.created_at.desc())
        )

    def persist_session(
        self,
        *,
        publication_run_id: UUID,
        upload_uri: SecretValue,
        total_bytes: int,
        chunk_bytes: int,
        expires_at: datetime | None,
        provider_attempt_id: UUID | None = None,
    ) -> YouTubeUploadSession:
        """Seal and store a resumable session. Called *before* any media byte.

        An interrupted worker that never reaches this point has created a
        session YouTube will simply expire; one that reaches it can always
        resume instead of starting a second upload.
        """
        context = session_context(publication_run_id)
        sealed = self.keyring.seal(
            upload_uri.reveal(), purpose=PURPOSE_SESSION_URI, context=context
        )
        row = YouTubeUploadSession(
            publication_run_id=publication_run_id,
            session_uri_ciphertext=sealed.ciphertext,
            session_uri_nonce=sealed.nonce,
            session_uri_hash=uri_hash(upload_uri.reveal()),
            encryption_key_version=sealed.key_version,
            total_bytes=total_bytes,
            confirmed_offset=0,
            chunk_bytes=chunk_bytes,
            status="active",
            expires_at=expires_at,
            provider_attempt_id=provider_attempt_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def session_uri(self, row: YouTubeUploadSession) -> SecretValue:
        return self.keyring.open(
            SealedSecret(
                ciphertext=row.session_uri_ciphertext,
                nonce=row.session_uri_nonce,
                key_version=row.encryption_key_version,
                purpose=PURPOSE_SESSION_URI,
            ),
            context=session_context(row.publication_run_id),
        )

    def confirm_offset(
        self, row: YouTubeUploadSession, offset: int, *, response_code: int | None = None
    ) -> None:
        """Advance to a *server-confirmed* offset. Never rewinds it.

        A server that reports a lower offset than we already recorded is
        answering an older question; taking the maximum keeps a stale response
        from re-sending bytes YouTube already has.
        """
        if offset < 0 or offset > row.total_bytes:
            raise ValueError("a confirmed offset must lie within the declared total size")
        row.confirmed_offset = max(int(row.confirmed_offset), offset)
        row.last_response_code = response_code
        row.last_confirmed_at = datetime.now(UTC)
        self.session.flush()

    def complete_session(self, row: YouTubeUploadSession, video_id: str) -> None:
        row.confirmed_offset = row.total_bytes
        row.video_id = video_id
        row.status = "completed"
        row.last_confirmed_at = datetime.now(UTC)
        self.session.flush()

    def mark_session(self, row: YouTubeUploadSession, status: str) -> None:
        if status not in {"active", "completed", "expired", "ambiguous", "cancelled"}:
            raise ValueError(f"unknown upload-session status {status!r}")
        row.status = status
        self.session.flush()

    # -- publication assets --------------------------------------------------
    def asset(
        self, publication_run_id: UUID, kind: PublicationAssetKind
    ) -> PublicationAsset | None:
        return self.session.scalar(
            select(PublicationAsset).where(
                PublicationAsset.publication_run_id == publication_run_id,
                PublicationAsset.kind == kind.value,
            )
        )

    def assets_for(self, publication_run_id: UUID) -> Sequence[PublicationAsset]:
        return self.session.scalars(
            select(PublicationAsset)
            .where(PublicationAsset.publication_run_id == publication_run_id)
            .order_by(PublicationAsset.created_at)
        ).all()

    def succeeded_caption(
        self, publication_run_id: UUID, language: str, name: str
    ) -> PublicationAsset | None:
        return self.session.scalar(
            select(PublicationAsset).where(
                PublicationAsset.publication_run_id == publication_run_id,
                PublicationAsset.kind == PublicationAssetKind.CAPTION.value,
                PublicationAsset.language == language,
                PublicationAsset.name == name,
                PublicationAsset.status == PublicationAssetStatus.SUCCEEDED.value,
            )
        )

    def upsert_asset(
        self,
        *,
        publication_run_id: UUID,
        kind: PublicationAssetKind,
        status: PublicationAssetStatus,
        local_asset_id: UUID | None = None,
        local_asset_sha256: str | None = None,
        provider_resource_id: str | None = None,
        provider_attempt_id: UUID | None = None,
        language: str = "",
        name: str = "",
        byte_size: int = 0,
        projection: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> PublicationAsset:
        """Create or update the single row for this run and kind.

        Captions are keyed by language and name as well, so two languages are
        two rows while the same language twice is one.
        """
        query = select(PublicationAsset).where(
            PublicationAsset.publication_run_id == publication_run_id,
            PublicationAsset.kind == kind.value,
        )
        if kind is PublicationAssetKind.CAPTION:
            query = query.where(
                PublicationAsset.language == language, PublicationAsset.name == name
            )
        row = self.session.scalar(query)
        if row is None:
            row = PublicationAsset(
                publication_run_id=publication_run_id,
                kind=kind.value,
                language=language,
                name=name,
            )
            self.session.add(row)
        row.status = status.value
        row.local_asset_id = local_asset_id
        row.local_asset_sha256 = local_asset_sha256
        if provider_resource_id:
            row.provider_resource_id = provider_resource_id
        if provider_attempt_id is not None:
            row.provider_attempt_id = provider_attempt_id
        row.byte_size = byte_size
        row.projection = projection or {}
        row.error_code = error_code[:128] if error_code else None
        row.error_summary = error_summary[:500] if error_summary else None
        if status is PublicationAssetStatus.SUCCEEDED:
            row.completed_at = row.completed_at or datetime.now(UTC)
        self.session.flush()
        return row
