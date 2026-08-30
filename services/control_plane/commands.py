"""The API-facing half of the T18b control plane.

A route calls exactly one method here, inside its own request transaction, and
gets back a command projection it can return. By the time it does, the command
row exists, is claimable by a dispatcher, and is queryable by ID - which is what
makes ``202 Accepted`` true rather than aspirational.

Nothing in this module talks to Temporal or to a provider. Dispatch is the
dispatcher's job; this is the durable record that dispatch will act on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from services.control_plane.lineage import LineageUnavailable, upstream_identity
from services.control_plane.status import command_projection
from vidgen.contracts.control_commands import (
    MAX_METADATA_VALUE_LENGTH,
    ControlCommand,
    ControlCommandRequest,
    ControlCommandStatus,
    ControlCommandTargetType,
    ControlCommandType,
)
from vidgen.contracts.review import ApiErrorCode, PipelineStage
from vidgen.db.control_command_repository import (
    ControlCommandError,
    ControlCommandRepository,
)
from vidgen.db.models import Project
from vidgen.review.errors import ReviewError, conflict, not_found
from vidgen.review.events import ProjectEventService

#: Which pipeline stage each command belongs to, for the project event stream.
#: Statuses where a real workflow may already be running, so a cancellation
#: has to reach the cluster rather than only the row. ``dispatching`` is
#: included because the start may have succeeded and the crash may have come
#: before the row recorded it.
_DISPATCHED_OR_STARTING = frozenset(
    {
        ControlCommandStatus.DISPATCHING,
        ControlCommandStatus.RUNNING,
        ControlCommandStatus.AWAITING_REVIEW,
    }
)

_COMMAND_STAGE: dict[ControlCommandType, PipelineStage] = {
    ControlCommandType.REFERENCE_BUILD: PipelineStage.KEYFRAMES,
    ControlCommandType.REFERENCE_GENERATE: PipelineStage.KEYFRAMES,
    ControlCommandType.REFERENCE_APPLY: PipelineStage.KEYFRAMES,
    ControlCommandType.SHOT_REGENERATE: PipelineStage.SHOT_ORCHESTRATION,
    ControlCommandType.SHOT_RETRY: PipelineStage.SHOT_ORCHESTRATION,
    ControlCommandType.SHOT_REVIEW_CONTINUE: PipelineStage.SHOT_ORCHESTRATION,
    ControlCommandType.TRANSCRIPT_REVISION: PipelineStage.TRANSCRIPT_ACQUISITION,
    ControlCommandType.SCRIPT_REVISION: PipelineStage.SCRIPT_GENERATION,
    ControlCommandType.FINAL_QA_RUN: PipelineStage.REVIEW,
    ControlCommandType.FINAL_QA_REMEDIATION: PipelineStage.REVIEW,
    ControlCommandType.RENDER_RERENDER: PipelineStage.RENDERING,
    ControlCommandType.PROJECT_CONTINUE: PipelineStage.REVIEW,
}


def request_digest(payload: object) -> str:
    """Bind an idempotency key to the request material that first used it."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """The created or adopted command, and whether this call created it."""

    command: ControlCommand
    created: bool


class ControlPlaneService:
    def __init__(self, session: Session, owner_subject: str) -> None:
        self._session = session
        self._owner = owner_subject
        self._repository = ControlCommandRepository(session)
        self._events = ProjectEventService(session)

    # -- reads ------------------------------------------------------------
    def list_commands(self, project: Project, *, limit: int = 100) -> list[ControlCommand]:
        return [
            command_projection(record)
            for record in self._repository.list_for_project(project.id, limit=limit)
        ]

    def get_command(self, project: Project, command_id: UUID) -> ControlCommand:
        record = self._repository.get(project.id, command_id)
        if record is None or record.owner_subject != self._owner:
            raise not_found("command")
        return command_projection(record)

    # -- writes -----------------------------------------------------------
    def submit(
        self,
        project: Project,
        *,
        command_type: ControlCommandType,
        target_type: ControlCommandTargetType,
        target_id: UUID,
        idempotency_key: str,
        payload: object,
        expected_row_version: int | None = None,
        metadata: dict[str, str] | None = None,
        entry_stage: str = "upload",
        shot_identity_hash: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> CommandOutcome:
        """Persist one command, or adopt the identical one already recorded.

        The upstream identity is resolved *before* the row is written: a command
        the project cannot currently produce an identity for is refused with an
        actionable precondition rather than accepted and failed later.
        """
        try:
            identity = upstream_identity(
                self._session,
                project_id=project.id,
                command_type=command_type,
                target_id=target_id,
                entry_stage=entry_stage,
                shot_identity_hash=shot_identity_hash,
            )
        except LineageUnavailable as error:
            raise conflict(ApiErrorCode.VALIDATION_FAILED, error.summary) from error
        request = ControlCommandRequest(
            project_id=project.id,
            owner_subject=self._owner,
            command_type=command_type,
            target_type=target_type,
            target_id=target_id,
            idempotency_key=idempotency_key[:255],
            request_hash=request_digest(payload),
            upstream_input_identity=identity,
            expected_row_version=expected_row_version,
            metadata=_bounded(metadata or {}),
            trace_context=_bounded(trace_context or {}, limit=8),
        )
        try:
            creation = self._repository.create(request)
        except ControlCommandError as error:
            raise ReviewError(
                409,
                conflict(ApiErrorCode.IDEMPOTENCY_KEY_MISMATCH, error.summary).error,
            ) from error
        if creation.created:
            self._events.append(
                project.id,
                event_type=f"command_{command_type.value}",
                status=ControlCommandStatus.PENDING.value,
                stage=_COMMAND_STAGE[command_type],
                payload={"command_id": str(creation.record.id)},
            )
        return CommandOutcome(command_projection(creation.record), creation.created)

    def cancel(self, project: Project, command_id: UUID) -> ControlCommand:
        """Stop a command, reaching the workflow when one was actually started.

        A command that has not been dispatched has nothing running behind it, so
        it is cancelled here and now. A dispatched command owns a live Temporal
        workflow that keeps spending until something tells it to stop, and this
        request thread is the wrong place to do that: it can die between writing
        the row and reaching the cluster. So the cancellation is *requested*
        durably and the dispatcher performs it; the command keeps its current
        status, and only reaches ``cancelled`` once the workflow has been.
        """
        record = self._repository.get(project.id, command_id)
        if record is None or record.owner_subject != self._owner:
            raise not_found("command")
        status = ControlCommandStatus(record.status)
        try:
            if status in _DISPATCHED_OR_STARTING:
                self._repository.request_cancellation(record)
            else:
                self._repository.cancel(record)
        except ControlCommandError as error:
            raise conflict(ApiErrorCode.VALIDATION_FAILED, error.summary) from error
        self._session.flush()
        return command_projection(record)

    def retry(self, project: Project, command_id: UUID) -> ControlCommand:
        record = self._repository.get(project.id, command_id)
        if record is None or record.owner_subject != self._owner:
            raise not_found("command")
        try:
            self._repository.requeue(record)
        except ControlCommandError as error:
            raise conflict(ApiErrorCode.VALIDATION_FAILED, error.summary) from error
        self._session.flush()
        return command_projection(record)


def _bounded(values: dict[str, str], *, limit: int = 24) -> dict[str, str]:
    """Truncate and cap a metadata map so a command row stays a command row."""
    bounded: dict[str, Any] = {}
    for key, value in sorted(values.items()):
        if len(bounded) >= limit:
            break
        bounded[str(key)[:64]] = str(value)[:MAX_METADATA_VALUE_LENGTH]
    return bounded
