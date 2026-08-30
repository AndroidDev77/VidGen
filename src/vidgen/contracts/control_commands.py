"""Compact, ID-only contracts for the T18b durable control plane.

Every asynchronous product command - build references, regenerate a shot,
resume a paused review, rebuild after a transcript edit, run T22, route a T22
remediation, continue a paused project - is persisted as one *control command*
before any route reports that it was accepted. These contracts are the only
shape that crosses the API, the database and Temporal, so they carry
identifiers, hashes, statuses and bounded counts and nothing else.

Three rules the shapes below enforce rather than document:

* No command payload may carry transcript text, script text, prompts, media,
  render manifests, provider responses or credentials. ``metadata`` is a bounded
  string map, and every reference to real content is an ID or a hash.
* A command's identity is ``(project_id, command_type, idempotency_key)`` and is
  bound to ``request_hash``. Replaying the same key with different request
  material is a conflict, never a silent second dispatch.
* A command is only ``running`` once a workflow actually exists, which is why
  ``workflow_id`` is required for every status past ``dispatching``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from vidgen.contracts.common import StrictContract

#: Upper bound on the redacted metadata a command may carry, so a command row
#: can never become an unbounded payload store.
MAX_METADATA_ENTRIES = 24
MAX_METADATA_VALUE_LENGTH = 256


class ControlCommandType(StrEnum):
    """Every product command T18b can durably dispatch.

    Adding a member without a handler is a registration failure the dispatcher
    tests catch: an unroutable command must never be accepted by an endpoint.
    """

    #: T19 - draft the project's continuity references and wait for approval.
    REFERENCE_BUILD = "reference_build"
    #: T19 - (re)generate one entity's reference sheet.
    REFERENCE_GENERATE = "reference_generate"
    #: T19 - bind approved references onto the affected shots.
    REFERENCE_APPLY = "reference_apply"
    #: T18 - start a real replacement child workflow for exactly one shot.
    SHOT_REGENERATE = "shot_regenerate"
    #: T21 - resume or restart a shot's interrupted repair lineage.
    SHOT_RETRY = "shot_retry"
    #: T20/T21 - continue a shot that is durably waiting on a human decision.
    SHOT_REVIEW_CONTINUE = "shot_review_continue"
    #: T18b - rebuild the downstream lineage a transcript edit invalidated.
    TRANSCRIPT_REVISION = "transcript_revision"
    #: T18b - rebuild the downstream lineage a script revision invalidated.
    SCRIPT_REVISION = "script_revision"
    #: T22 - run final editorial QA against the current selected render.
    FINAL_QA_RUN = "final_qa_run"
    #: T22 - hand confirmed findings to the stage that owns their repair.
    FINAL_QA_REMEDIATION = "final_qa_remediation"
    #: T17b - queue a rerender through the canonical render-job boundary.
    RENDER_RERENDER = "render_rerender"
    #: T18b - start a new project generation run after a pause or revision.
    PROJECT_CONTINUE = "project_continue"


class ControlCommandStatus(StrEnum):
    """The command lifecycle. Only ``pending`` precedes durable executable work."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


#: Statuses no transition may leave. A completion write against one of these is
#: idempotent rather than an error, so a duplicated worker callback is safe.
TERMINAL_STATUSES: frozenset[ControlCommandStatus] = frozenset(
    {
        ControlCommandStatus.COMPLETED,
        ControlCommandStatus.FAILED,
        ControlCommandStatus.CANCELLED,
        ControlCommandStatus.SUPERSEDED,
    }
)

#: Statuses that require a real Temporal workflow identity to be persisted. The
#: database CHECK constraint mirrors this exactly: a command cannot claim to be
#: running against a workflow ID nobody started.
DISPATCHED_STATUSES: frozenset[ControlCommandStatus] = frozenset(
    {
        ControlCommandStatus.RUNNING,
        ControlCommandStatus.AWAITING_REVIEW,
        ControlCommandStatus.COMPLETED,
    }
)

#: The legal status graph, enforced by the repository and by the migration.
ALLOWED_TRANSITIONS: dict[ControlCommandStatus, frozenset[ControlCommandStatus]] = {
    ControlCommandStatus.PENDING: frozenset(
        {
            ControlCommandStatus.CLAIMED,
            ControlCommandStatus.CANCELLED,
            ControlCommandStatus.SUPERSEDED,
        }
    ),
    ControlCommandStatus.CLAIMED: frozenset(
        {
            ControlCommandStatus.DISPATCHING,
            ControlCommandStatus.PENDING,
            ControlCommandStatus.FAILED,
            ControlCommandStatus.CANCELLED,
        }
    ),
    ControlCommandStatus.DISPATCHING: frozenset(
        {
            ControlCommandStatus.RUNNING,
            ControlCommandStatus.PENDING,
            ControlCommandStatus.FAILED,
            ControlCommandStatus.CANCELLED,
        }
    ),
    ControlCommandStatus.RUNNING: frozenset(
        {
            ControlCommandStatus.AWAITING_REVIEW,
            ControlCommandStatus.COMPLETED,
            ControlCommandStatus.FAILED,
            ControlCommandStatus.CANCELLED,
        }
    ),
    ControlCommandStatus.AWAITING_REVIEW: frozenset(
        {
            ControlCommandStatus.RUNNING,
            ControlCommandStatus.COMPLETED,
            ControlCommandStatus.FAILED,
            ControlCommandStatus.CANCELLED,
        }
    ),
    ControlCommandStatus.COMPLETED: frozenset(),
    ControlCommandStatus.FAILED: frozenset({ControlCommandStatus.PENDING}),
    ControlCommandStatus.CANCELLED: frozenset(),
    ControlCommandStatus.SUPERSEDED: frozenset(),
}


class ControlCommandTargetType(StrEnum):
    """What a command acts on. Every value maps to an existing owned resource."""

    PROJECT = "project"
    SHOT = "shot"
    CHARACTER = "character"
    LOCATION = "location"
    REFERENCE_SET = "reference_set"
    RENDER_JOB = "render_job"
    FINAL_QA_RUN = "final_qa_run"
    TRANSCRIPT = "transcript"
    SCRIPT = "script"
    REPAIR_RUN = "repair_run"
    GENERATION_RUN = "generation_run"


class ControlCommandRequest(StrictContract):
    """What a route hands the control plane to create exactly one command."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    owner_subject: str = Field(min_length=1, max_length=255)
    command_type: ControlCommandType
    target_type: ControlCommandTargetType
    target_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    #: The identity of the upstream material this command was calculated
    #: against. A dispatch whose upstream identity has moved is stale and is
    #: failed with an actionable code rather than executed against new inputs.
    upstream_input_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_row_version: int | None = Field(default=None, ge=1)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=MAX_METADATA_ENTRIES)
    trace_context: dict[str, str] = Field(default_factory=dict, max_length=8)

    @model_validator(mode="after")
    def bound_metadata(self) -> ControlCommandRequest:
        for key, value in self.metadata.items():
            if len(key) > 64 or len(value) > MAX_METADATA_VALUE_LENGTH:
                raise ValueError("control-command metadata entries are bounded and redacted")
        return self


class ControlCommandFailure(StrictContract):
    """A structured, renderable command failure. Never a traceback or SQL text."""

    schema_version: Literal["1.0"] = "1.0"
    code: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    attempt: int = Field(default=0, ge=0)


class ControlCommandProgress(StrictContract):
    """Bounded progress. A phase label and a percentage, never a payload."""

    schema_version: Literal["1.0"] = "1.0"
    phase: str = Field(default="", max_length=64)
    percent: int = Field(default=0, ge=0, le=100)
    #: Why the command is durably waiting, when it is. Empty otherwise.
    waiting_reason: str = Field(default="", max_length=128)


class ControlCommandResult(StrictContract):
    """The compact outcome a completed command persists."""

    schema_version: Literal["1.0"] = "1.0"
    result_type: ControlCommandTargetType | None = None
    result_id: UUID | None = None
    #: Bounded, redacted counts and identifiers a UI can render directly.
    summary: dict[str, str] = Field(default_factory=dict, max_length=MAX_METADATA_ENTRIES)


class ControlCommand(StrictContract):
    """The owner-facing projection of one durable command."""

    schema_version: Literal["1.0"] = "1.0"
    command_id: UUID
    project_id: UUID
    command_type: ControlCommandType
    status: ControlCommandStatus
    target_type: ControlCommandTargetType
    target_id: UUID
    workflow_id: str | None = Field(default=None, max_length=255)
    run_id: str | None = Field(default=None, max_length=255)
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    progress: ControlCommandProgress = Field(default_factory=ControlCommandProgress)
    result: ControlCommandResult | None = None
    failure: ControlCommandFailure | None = None
    row_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: What the owner may do next. Rendered as buttons; never inferred client-side.
    permitted_actions: list[Literal["cancel", "retry"]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def dispatched_commands_name_a_workflow(self) -> ControlCommand:
        if self.status in DISPATCHED_STATUSES and not self.workflow_id:
            raise ValueError(f"a {self.status} command must name the workflow that was started")
        return self


class ProjectGenerationRunStatus(StrEnum):
    """The lifecycle of one immutable generation attempt over a project."""

    ACTIVE = "active"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ProjectGenerationRun(StrictContract):
    """One immutable generation attempt: a project's unit of restartability.

    A material revision starts a new run rather than overwriting the previous
    one, so history stays readable and a resumed project never re-enters a
    completed workflow execution.
    """

    schema_version: Literal["1.0"] = "1.0"
    generation_run_id: UUID
    project_id: UUID
    sequence: int = Field(ge=1)
    status: ProjectGenerationRunStatus
    #: The earliest stage this run must execute. Everything before it is reused.
    entry_stage: str = Field(min_length=1, max_length=64)
    #: The hash binding this run to the exact upstream material it was started
    #: for. Two runs with the same identity are the same work.
    input_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_id: str | None = Field(default=None, max_length=255)
    run_id: str | None = Field(default=None, max_length=255)
    #: The command that started this run, when a human command did.
    origin_command_id: UUID | None = None
    parent_generation_run_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ProjectRevisionRequest(StrictContract):
    """A confirmed transcript or script revision, ready to be made durable."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    revision_kind: Literal["transcript", "script"]
    #: The transcript or script the edit produced.
    source_id: UUID
    entry_stage: str = Field(min_length=1, max_length=64)
    #: The exact invalidation set the owner confirmed, as stage names.
    confirmed_stages: list[str] = Field(default_factory=list, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=255)


class VoiceProfileSelection(StrictContract):
    """A project's narration voice, validated before a workflow may start.

    Deliberately credential-free: a profile names a provider and an externally
    provisioned voice, and the credential for that provider is resolved by the
    worker from configuration, never carried here.
    """

    schema_version: Literal["1.0"] = "1.0"
    voice_profile_id: UUID
    project_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=64)
    provider_voice_id: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=128)
    language: str = Field(min_length=1, max_length=32)
    profile_version: int = Field(ge=1)
    configuration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_format: str = Field(min_length=1, max_length=16)
    scope: Literal["project", "shared"]
    selected: bool = False


class WorkflowContinuationRequest(StrictContract):
    """Ask a paused or partially complete project to continue."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    #: The stage the new generation run must start from.
    entry_stage: str = Field(min_length=1, max_length=64)
    reason: Literal[
        "review_resolved",
        "partial_fanout",
        "revision",
        "remediation",
        "operator_request",
    ]
    idempotency_key: str = Field(min_length=1, max_length=255)


class WorkflowContinuationResult(StrictContract):
    """What continuing a project actually produced. Never a calculated ID."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    command_id: UUID
    generation_run_id: UUID
    status: ControlCommandStatus
    workflow_id: str | None = Field(default=None, max_length=255)
    entry_stage: str = Field(min_length=1, max_length=64)
    reused: bool = False
