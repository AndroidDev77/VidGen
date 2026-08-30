"""Transactional persistence, claiming and completion for T18b control commands.

The repository is the only place a command row changes status. Everything it
exposes is safe to call concurrently from several dispatcher replicas and safe
to call twice from the same one, because every transition is a conditional
``UPDATE`` guarded by the row version it read. That single pattern gives
PostgreSQL-safe claiming without a backend-specific statement, so the
deterministic SQLite tests exercise exactly the production code path; on
PostgreSQL the candidate scan additionally takes ``FOR UPDATE SKIP LOCKED`` so
replicas do not queue behind each other.

Nothing here talks to Temporal. Dispatch lives in ``services.control_plane``;
this module only records what dispatch did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Update, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ControlCommandFailure,
    ControlCommandProgress,
    ControlCommandRequest,
    ControlCommandResult,
    ControlCommandStatus,
    ControlCommandType,
)
from vidgen.db.control_command_models import ControlCommandRecord

#: How long a claim is valid before another dispatcher may take it over. Long
#: enough for a slow Temporal start, short enough that a killed replica does not
#: strand a command for minutes.
DEFAULT_LEASE_SECONDS = 120


class ControlCommandError(RuntimeError):
    """A command could not be created or transitioned as requested."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


@dataclass(frozen=True, slots=True)
class CommandCreation:
    """The created (or adopted) command, and which of the two it was."""

    record: ControlCommandRecord
    created: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; comparisons must stay UTC-aware."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class ControlCommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _update(self, statement: Update) -> int:
        """Run one guarded UPDATE and return how many rows it actually changed.

        Every transition in this repository is conditional on the row version it
        read, so the row count *is* the concurrency answer: one means this caller
        won, zero means someone else moved the command first.
        """
        result = cast("CursorResult[Any]", self._session.execute(statement))
        return int(result.rowcount)

    # -- creation ---------------------------------------------------------
    def create(self, request: ControlCommandRequest) -> CommandCreation:
        """Persist one command, or adopt the identical one already present.

        Called inside the request transaction, so a route that reports
        acceptance has already committed executable work. Reusing a key with
        different request material raises rather than silently dispatching the
        first request a second time.
        """
        existing = self._session.scalar(
            select(ControlCommandRecord).where(
                ControlCommandRecord.project_id == request.project_id,
                ControlCommandRecord.command_type == request.command_type.value,
                ControlCommandRecord.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            return CommandCreation(self._verify_replay(existing, request), created=False)
        record = ControlCommandRecord(
            project_id=request.project_id,
            owner_subject=request.owner_subject,
            command_type=request.command_type.value,
            target_type=request.target_type.value,
            target_id=request.target_id,
            idempotency_key=request.idempotency_key,
            request_hash=request.request_hash,
            upstream_input_identity=request.upstream_input_identity,
            expected_row_version=request.expected_row_version,
            status=ControlCommandStatus.PENDING.value,
            attempt=0,
            command_metadata=dict(request.metadata),
            trace_context=dict(request.trace_context),
            available_at=_now(),
        )
        # The insert runs inside a SAVEPOINT. A losing race must undo *this
        # insert* and nothing else: the caller is mid-request, and the edit that
        # prompted this command - a transcript segment, a script selection - is
        # already in the same transaction. Rolling that back while still
        # returning 200 would silently discard the owner's work.
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            # Two concurrent submissions of the same key. The winner's row is
            # the command; this request adopts it rather than creating a second.
            adopted = self._session.scalar(
                select(ControlCommandRecord).where(
                    ControlCommandRecord.project_id == request.project_id,
                    ControlCommandRecord.command_type == request.command_type.value,
                    ControlCommandRecord.idempotency_key == request.idempotency_key,
                )
            )
            if adopted is None:  # pragma: no cover - the constraint fired for another reason
                raise
            return CommandCreation(self._verify_replay(adopted, request), created=False)
        return CommandCreation(record, created=True)

    def _verify_replay(
        self, record: ControlCommandRecord, request: ControlCommandRequest
    ) -> ControlCommandRecord:
        if record.request_hash != request.request_hash:
            raise ControlCommandError(
                "command_idempotency_mismatch",
                "This idempotency key was already used for a different command request.",
            )
        if record.owner_subject != request.owner_subject:
            # Indistinguishable from a mismatch on purpose: one owner must not
            # learn that another owner used the same key.
            raise ControlCommandError(
                "command_idempotency_mismatch",
                "This idempotency key was already used for a different command request.",
            )
        return record

    # -- reads ------------------------------------------------------------
    def get(self, project_id: UUID, command_id: UUID) -> ControlCommandRecord | None:
        return self._session.scalar(
            select(ControlCommandRecord).where(
                ControlCommandRecord.id == command_id,
                ControlCommandRecord.project_id == project_id,
            )
        )

    def list_for_project(self, project_id: UUID, *, limit: int = 100) -> list[ControlCommandRecord]:
        return list(
            self._session.scalars(
                select(ControlCommandRecord)
                .where(ControlCommandRecord.project_id == project_id)
                .order_by(ControlCommandRecord.created_at.desc(), ControlCommandRecord.id.desc())
                .limit(limit)
            )
        )

    def active_for_target(
        self, project_id: UUID, command_type: ControlCommandType, target_id: UUID
    ) -> ControlCommandRecord | None:
        """The command already driving this target, if one is still in flight."""
        return self._session.scalar(
            select(ControlCommandRecord)
            .where(
                ControlCommandRecord.project_id == project_id,
                ControlCommandRecord.command_type == command_type.value,
                ControlCommandRecord.target_id == target_id,
                ControlCommandRecord.status.notin_([status.value for status in TERMINAL_STATUSES]),
            )
            .order_by(ControlCommandRecord.created_at.desc())
        )

    # -- claiming ---------------------------------------------------------
    def claimable(self, *, limit: int, now: datetime | None = None) -> list[ControlCommandRecord]:
        """Candidate commands: pending and due, or holding an expired lease.

        A lease that has expired is recovered rather than abandoned; the worker
        that held it may be gone, and the command's own attempt bound is what
        stops a genuinely poisonous command from cycling forever. That covers
        ``dispatching`` as well as ``claimed``: a dispatcher can die at either
        point, and re-running the handler adopts the deterministic workflow
        instead of starting a second one.
        """
        moment = now or _now()
        statement = (
            select(ControlCommandRecord)
            .where(
                ControlCommandRecord.status.in_(
                    [
                        ControlCommandStatus.PENDING.value,
                        ControlCommandStatus.CLAIMED.value,
                        # A dispatcher killed between taking the lease and
                        # recording the started workflow leaves the row here.
                        # Without this it would never be looked at again, so
                        # the command would be permanently stranded.
                        ControlCommandStatus.DISPATCHING.value,
                    ]
                ),
                ControlCommandRecord.attempt < ControlCommandRecord.max_attempts,
            )
            .order_by(ControlCommandRecord.created_at, ControlCommandRecord.id)
            .limit(limit)
        )
        if self._session.bind is not None and self._session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        candidates = []
        for record in self._session.scalars(statement):
            if record.status == ControlCommandStatus.PENDING.value:
                available = _aware(record.available_at)
                if available is not None and available > moment:
                    continue
                candidates.append(record)
                continue
            lease = _aware(record.lease_expires_at)
            if lease is not None and lease <= moment:
                candidates.append(record)
        return candidates

    def claim(
        self,
        record: ControlCommandRecord,
        *,
        claim_owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """Take the lease on one command. ``False`` means another replica won.

        The guard is the row version that was read, so two dispatchers claiming
        the same candidate cannot both succeed and cannot both dispatch.
        """
        moment = now or _now()
        expected_version = record.row_version
        changed = self._update(
            update(ControlCommandRecord)
            .where(
                ControlCommandRecord.id == record.id,
                ControlCommandRecord.row_version == expected_version,
                ControlCommandRecord.status.in_(
                    [
                        ControlCommandStatus.PENDING.value,
                        ControlCommandStatus.CLAIMED.value,
                        ControlCommandStatus.DISPATCHING.value,
                    ]
                ),
                ControlCommandRecord.attempt < ControlCommandRecord.max_attempts,
            )
            .values(
                status=ControlCommandStatus.CLAIMED.value,
                claim_owner=claim_owner[:128],
                lease_expires_at=moment + timedelta(seconds=lease_seconds),
                attempt=ControlCommandRecord.attempt + 1,
                row_version=expected_version + 1,
                updated_at=moment,
            )
        )
        if changed != 1:
            return False
        self._session.expire(record)
        return True

    def heartbeat(
        self,
        record: ControlCommandRecord,
        *,
        claim_owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """Extend the lease of a command this dispatcher still owns."""
        moment = now or _now()
        changed = self._update(
            update(ControlCommandRecord)
            .where(
                ControlCommandRecord.id == record.id,
                ControlCommandRecord.claim_owner == claim_owner[:128],
                ControlCommandRecord.status.notin_([status.value for status in TERMINAL_STATUSES]),
            )
            .values(lease_expires_at=moment + timedelta(seconds=lease_seconds), updated_at=moment)
        )
        self._session.expire(record)
        return changed == 1

    # -- transitions ------------------------------------------------------
    def _transition(
        self,
        record: ControlCommandRecord,
        target: ControlCommandStatus,
        values: dict[str, object],
        *,
        now: datetime | None = None,
    ) -> bool:
        current = ControlCommandStatus(record.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            if current == target:
                # Idempotent completion: a duplicated worker callback must not
                # be an error, and must not write the row a second time.
                return False
            raise ControlCommandError(
                "command_invalid_transition",
                f"A {current.value} command cannot become {target.value}.",
            )
        moment = now or _now()
        expected_version = record.row_version
        changed = self._update(
            update(ControlCommandRecord)
            .where(
                ControlCommandRecord.id == record.id,
                ControlCommandRecord.row_version == expected_version,
                ControlCommandRecord.status == current.value,
            )
            .values(
                status=target.value,
                row_version=expected_version + 1,
                updated_at=moment,
                **values,
            )
        )
        self._session.expire(record)
        return changed == 1

    def mark_dispatching(self, record: ControlCommandRecord) -> bool:
        return self._transition(
            record, ControlCommandStatus.DISPATCHING, {"progress_phase": "dispatching"}
        )

    def mark_running(
        self,
        record: ControlCommandRecord,
        *,
        workflow_id: str,
        run_id: str | None,
        progress: ControlCommandProgress | None = None,
    ) -> bool:
        """Record the real workflow identity and only then report running.

        ``workflow_id`` is required, and the database rejects the write without
        it: this is the point where an accepted command stops being a promise.
        """
        if not workflow_id:
            raise ControlCommandError(
                "command_dispatch_identity_missing",
                "A command cannot run without the identity of a started workflow.",
            )
        moment = _now()
        detail = progress or ControlCommandProgress(phase="running", percent=5)
        return self._transition(
            record,
            ControlCommandStatus.RUNNING,
            {
                "workflow_id": workflow_id[:255],
                "run_id": run_id[:255] if run_id else None,
                "dispatched_at": record.dispatched_at or moment,
                "started_at": moment,
                "progress_phase": detail.phase,
                "progress_percent": detail.percent,
                "waiting_reason": "",
                "error_code": None,
                "error_summary": None,
            },
            now=moment,
        )

    def mark_awaiting_review(self, record: ControlCommandRecord, *, reason: str) -> bool:
        return self._transition(
            record,
            ControlCommandStatus.AWAITING_REVIEW,
            {"waiting_reason": reason[:128], "progress_phase": "awaiting_review"},
        )

    def mark_progress(self, record: ControlCommandRecord, progress: ControlCommandProgress) -> None:
        """Update bounded progress without changing the command's status."""
        self._session.execute(
            update(ControlCommandRecord)
            .where(ControlCommandRecord.id == record.id)
            .values(
                progress_phase=progress.phase,
                progress_percent=progress.percent,
                waiting_reason=progress.waiting_reason,
                updated_at=_now(),
            )
        )
        self._session.expire(record)

    def complete(self, record: ControlCommandRecord, result: ControlCommandResult) -> bool:
        moment = _now()
        return self._transition(
            record,
            ControlCommandStatus.COMPLETED,
            {
                "result_type": result.result_type.value if result.result_type else None,
                "result_id": result.result_id,
                "result_summary": dict(result.summary),
                "completed_at": moment,
                "claim_owner": None,
                "lease_expires_at": None,
                "progress_phase": "completed",
                "progress_percent": 100,
                "waiting_reason": "",
            },
            now=moment,
        )

    def fail(self, record: ControlCommandRecord, failure: ControlCommandFailure) -> bool:
        """Fail a command, or release it for another bounded attempt.

        A retryable failure below the attempt bound goes back to ``pending``
        with a backoff, which is what makes an interrupted dispatcher recover
        rather than strand its command. Everything else is terminal and carries
        an actionable code.
        """
        moment = _now()
        retry = failure.retryable and record.attempt < record.max_attempts
        if retry:
            return self._transition(
                record,
                ControlCommandStatus.PENDING,
                {
                    "claim_owner": None,
                    "lease_expires_at": None,
                    "available_at": moment + timedelta(seconds=min(60, 2**record.attempt)),
                    "error_code": failure.code[:128],
                    "error_summary": failure.summary[:500],
                    "retryable": True,
                },
                now=moment,
            )
        return self._transition(
            record,
            ControlCommandStatus.FAILED,
            {
                "claim_owner": None,
                "lease_expires_at": None,
                "completed_at": moment,
                "error_code": failure.code[:128],
                "error_summary": failure.summary[:500],
                "retryable": failure.retryable,
                "progress_phase": "failed",
            },
            now=moment,
        )

    def cancel(self, record: ControlCommandRecord, *, reason: str = "cancelled_by_owner") -> bool:
        moment = _now()
        return self._transition(
            record,
            ControlCommandStatus.CANCELLED,
            {
                "claim_owner": None,
                "lease_expires_at": None,
                "completed_at": moment,
                "error_code": reason[:128],
                "error_summary": "The owner cancelled this command.",
                "progress_phase": "cancelled",
            },
            now=moment,
        )

    def request_cancellation(
        self, record: ControlCommandRecord, *, now: datetime | None = None
    ) -> bool:
        """Record durably that the owner wants a dispatched command stopped.

        The request thread must not be what cancels a Temporal workflow: it can
        die between marking the row and reaching the cluster, and the command
        would then read ``cancelled`` while the workflow kept spending. The
        dispatcher owns the cancellation, and this row is what tells it to.

        ``False`` means the request changed nothing - the command already had a
        cancellation pending, or it moved on before the update landed.
        """
        moment = now or _now()
        changed = self._update(
            update(ControlCommandRecord)
            .where(
                ControlCommandRecord.id == record.id,
                ControlCommandRecord.row_version == record.row_version,
                ControlCommandRecord.cancel_requested_at.is_(None),
                ControlCommandRecord.status.in_(
                    [
                        ControlCommandStatus.DISPATCHING.value,
                        ControlCommandStatus.RUNNING.value,
                        ControlCommandStatus.AWAITING_REVIEW.value,
                    ]
                ),
            )
            .values(
                cancel_requested_at=moment,
                waiting_reason="cancellation_requested",
                row_version=record.row_version + 1,
                updated_at=moment,
            )
        )
        self._session.expire(record)
        return changed == 1

    def cancellation_requested(self, *, limit: int = 50) -> list[ControlCommandRecord]:
        """Dispatched commands whose owner has asked for them to stop."""
        return list(
            self._session.scalars(
                select(ControlCommandRecord)
                .where(
                    ControlCommandRecord.cancel_requested_at.is_not(None),
                    ControlCommandRecord.status.in_(
                        [
                            ControlCommandStatus.DISPATCHING.value,
                            ControlCommandStatus.RUNNING.value,
                            ControlCommandStatus.AWAITING_REVIEW.value,
                        ]
                    ),
                )
                .order_by(ControlCommandRecord.cancel_requested_at, ControlCommandRecord.id)
                .limit(limit)
            )
        )

    def supersede(self, record: ControlCommandRecord, *, reason: str) -> bool:
        return self._transition(
            record,
            ControlCommandStatus.SUPERSEDED,
            {
                "claim_owner": None,
                "lease_expires_at": None,
                "completed_at": _now(),
                "error_code": reason[:128],
                "error_summary": "A newer command replaced this one.",
            },
        )

    def requeue(self, record: ControlCommandRecord) -> bool:
        """Return a failed command to the queue for an explicit owner retry."""
        if ControlCommandStatus(record.status) is not ControlCommandStatus.FAILED:
            raise ControlCommandError(
                "command_not_retryable",
                "Only a failed command can be retried.",
            )
        if record.attempt >= record.max_attempts:
            # Give the operator's explicit retry one further bounded attempt
            # rather than silently accepting a command that can never run.
            record.max_attempts = record.max_attempts + 1
            self._session.flush()
        return self._transition(
            record,
            ControlCommandStatus.PENDING,
            {
                "available_at": _now(),
                "claim_owner": None,
                "lease_expires_at": None,
                "error_code": None,
                "error_summary": None,
                "completed_at": None,
                "progress_phase": "pending",
            },
        )
