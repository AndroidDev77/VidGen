"""T18b durable control commands and immutable project generation runs.

``control_commands`` is the single durable record behind every asynchronous
product command. A route persists one row inside the request transaction and
only then reports acceptance; a dispatcher claims it under a lease, starts or
signals the real workflow, writes the *actual* workflow identity back, and
drives it to a terminal state.

The constraints below are the invariant, not the documentation of one:

* ``uq_control_commands_idempotency`` makes a command idempotent per project and
  type, so a duplicated browser submission adopts the first row.
* ``control_command_dispatched_identity`` refuses to store a ``running``,
  ``awaiting_review`` or ``completed`` command that does not name a workflow -
  the database itself rejects a calculated-but-never-started workflow ID.
* ``control_command_active_claim`` ties a claim owner and a lease expiry
  together, so a claimed row is always recoverable by its lease.
* ``control_command_completed_result`` requires a completed command to carry the
  resource it produced, where its type produces one.

``project_generation_runs`` records each immutable generation attempt over a
project. A revision or a continuation creates a new run rather than mutating the
previous one, which is what lets a project resume without ever re-entering a
Temporal execution that has already closed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vidgen.contracts.control_commands import (
    ControlCommandStatus,
    ControlCommandType,
    ProjectGenerationRunStatus,
)
from vidgen.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Command types whose completion must name the resource they produced. A
#: command that only signals an existing workflow (a review continuation, a
#: retry) legitimately produces no new resource.
_RESULT_BEARING = (
    ControlCommandType.REFERENCE_BUILD,
    ControlCommandType.SHOT_REGENERATE,
    ControlCommandType.FINAL_QA_RUN,
    ControlCommandType.RENDER_RERENDER,
    ControlCommandType.PROJECT_CONTINUE,
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_STATUSES = tuple(status.value for status in ControlCommandStatus)
_TYPES = tuple(command.value for command in ControlCommandType)
_DISPATCHED = ("running", "awaiting_review", "completed")


class ControlCommandRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable, claimable, restartable product command."""

    __tablename__ = "control_commands"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    #: The authorized actor. Re-validated at dispatch: a command must not
    #: outlive the ownership that authorized it.
    owner_subject: Mapped[str] = mapped_column(String(255), index=True)
    command_type: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[UUID] = mapped_column(index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    #: Binds the key to the request that first used it.
    request_hash: Mapped[str] = mapped_column(String(64))
    #: The upstream material identity this command was calculated against.
    upstream_input_identity: Mapped[str] = mapped_column(String(64))
    expected_row_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default=ControlCommandStatus.PENDING.value)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    #: The dispatcher instance currently holding the lease, and when it expires.
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The workflow that was actually started or signalled. Never calculated.
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(255))
    result_type: Mapped[str | None] = mapped_column(String(32))
    result_id: Mapped[UUID | None] = mapped_column()
    #: Bounded, redacted string maps. Never a payload, prompt or credential.
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    command_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trace_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress_phase: Mapped[str] = mapped_column(String(64), default="")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    waiting_reason: Mapped[str] = mapped_column(String(128), default="")
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    generation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_generation_runs.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "command_type",
            "idempotency_key",
            name="uq_control_commands_idempotency",
        ),
        Index("ix_control_commands_claimable", "status", "available_at"),
        Index("ix_control_commands_project_status", "project_id", "status"),
        CheckConstraint(f"status IN ({_sql_list(_STATUSES)})", name="control_command_status"),
        CheckConstraint(f"command_type IN ({_sql_list(_TYPES)})", name="control_command_type"),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="control_command_attempts"),
        CheckConstraint("row_version >= 1", name="control_command_row_version"),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="control_command_progress",
        ),
        CheckConstraint("length(request_hash) = 64", name="control_command_request_hash"),
        CheckConstraint(
            "length(upstream_input_identity) = 64", name="control_command_upstream_identity"
        ),
        # A claim is a lease: both halves are present, or neither is.
        CheckConstraint(
            "(claim_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (claim_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="control_command_active_claim",
        ),
        # The core T18b invariant, enforced by the database rather than by a
        # convention: nothing may report itself dispatched without the identity
        # of the workflow that was actually started.
        CheckConstraint(
            f"status NOT IN ({_sql_list(_DISPATCHED)}) OR workflow_id IS NOT NULL",
            name="control_command_dispatched_identity",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="control_command_failure_code",
        ),
        CheckConstraint(
            "status <> 'completed' "
            f"OR command_type NOT IN ({_sql_list(tuple(t.value for t in _RESULT_BEARING))}) "
            "OR result_id IS NOT NULL",
            name="control_command_completed_result",
        ),
    )


class ProjectGenerationRunRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable generation attempt over a project.

    The partial unique index allows exactly one non-terminal run per project:
    two concurrent revisions cannot both claim to be the project's active
    lineage, while every historical run is preserved for audit.
    """

    __tablename__ = "project_generation_runs"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default=ProjectGenerationRunStatus.ACTIVE.value)
    entry_stage: Mapped[str] = mapped_column(String(64))
    input_identity: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(255))
    origin_command_id: Mapped[UUID | None] = mapped_column()
    parent_generation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_generation_runs.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("project_id", "sequence", name="uq_generation_run_sequence"),
        Index(
            "uq_generation_run_active",
            "project_id",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active"),
        ),
        CheckConstraint("sequence >= 1", name="generation_run_sequence"),
        CheckConstraint("length(input_identity) = 64", name="generation_run_identity"),
        CheckConstraint(
            "status IN ("
            + _sql_list(tuple(status.value for status in ProjectGenerationRunStatus))
            + ")",
            name="generation_run_status",
        ),
        # A run that is no longer the project's lineage cannot still be active,
        # and an active run cannot be terminal. Written with the boolean itself
        # rather than a comparison to 0 or 1, which PostgreSQL rejects.
        CheckConstraint(
            "(NOT active AND status IN ('completed','failed','cancelled','superseded')) "
            "OR (active AND status IN ('active','awaiting_review'))",
            name="generation_run_active_status",
        ),
    )
