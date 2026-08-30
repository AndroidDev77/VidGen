"""Owner-facing projections of a durable control command.

A command row carries a claim owner, a lease, a request hash and a trace
context. None of that belongs in a browser, so the projection here is
deliberately narrower than the row: identifiers, status, bounded progress, the
real workflow identity once one exists, a structured failure, and the actions
the owner may actually take next.
"""

from __future__ import annotations

from vidgen.contracts.control_commands import (
    TERMINAL_STATUSES,
    ControlCommand,
    ControlCommandFailure,
    ControlCommandProgress,
    ControlCommandResult,
    ControlCommandStatus,
    ControlCommandTargetType,
    ControlCommandType,
)
from vidgen.db.control_command_models import ControlCommandRecord


def permitted_actions(record: ControlCommandRecord) -> list[str]:
    """What the owner may do to this command right now.

    Computed here rather than in the browser so a UI can render buttons from
    the response instead of inferring them from a status string.
    """
    status = ControlCommandStatus(record.status)
    if status is ControlCommandStatus.FAILED:
        return ["retry"]
    if status in TERMINAL_STATUSES:
        return []
    return ["cancel"]


def command_projection(record: ControlCommandRecord) -> ControlCommand:
    failure = (
        ControlCommandFailure(
            code=record.error_code,
            summary=record.error_summary or "This command failed.",
            retryable=record.retryable,
            attempt=record.attempt,
        )
        if record.error_code and ControlCommandStatus(record.status) is ControlCommandStatus.FAILED
        else None
    )
    result = (
        ControlCommandResult(
            result_type=(
                ControlCommandTargetType(record.result_type) if record.result_type else None
            ),
            result_id=record.result_id,
            summary={
                key: str(value)[:256] for key, value in dict(record.result_summary or {}).items()
            },
        )
        if record.result_id or record.result_summary
        else None
    )
    return ControlCommand(
        command_id=record.id,
        project_id=record.project_id,
        command_type=ControlCommandType(record.command_type),
        status=ControlCommandStatus(record.status),
        target_type=ControlCommandTargetType(record.target_type),
        target_id=record.target_id,
        workflow_id=record.workflow_id,
        run_id=record.run_id,
        attempt=record.attempt,
        max_attempts=record.max_attempts,
        progress=ControlCommandProgress(
            phase=record.progress_phase or "",
            percent=record.progress_percent,
            waiting_reason=record.waiting_reason or "",
        ),
        result=result,
        failure=failure,
        row_version=record.row_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        dispatched_at=record.dispatched_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        permitted_actions=permitted_actions(record),  # type: ignore[arg-type]
    )
