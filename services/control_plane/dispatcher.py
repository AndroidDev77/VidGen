"""The worker that turns durable control commands into real workflows.

The dispatcher is a bounded database-polling worker rather than a Temporal
workflow, deliberately: its whole job is to read rows, start workflows and write
identities back, and none of that belongs in workflow history. What it borrows
from Temporal is the discipline - a lease, a heartbeat, bounded attempts, an
explicit terminal state and a graceful shutdown.

One pass does exactly this, per command:

1. claim it transactionally, so two replicas cannot both dispatch it;
2. revalidate ownership and upstream lineage;
3. start or signal the correct workflow;
4. persist the workflow and run identity that actually came back;
5. mark it running - never before step 4 succeeded;
6. later, poll that workflow's durable state and settle the command.

An interruption at any point is recoverable: the command either still holds a
lease that will expire, or is already ``running`` against a workflow that is
itself durable. Neither case dispatches twice.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from services.control_plane.generation_runs import GenerationRunService
from services.control_plane.handlers import (
    DispatchContext,
    DispatchFailure,
    dispatch,
)
from services.control_plane.status import command_projection
from vidgen.contracts.continuity_workflow import ReferenceWorkflowStatus
from vidgen.contracts.control_commands import (
    ControlCommand,
    ControlCommandFailure,
    ControlCommandProgress,
    ControlCommandResult,
    ControlCommandStatus,
    ControlCommandType,
    ProjectGenerationRunStatus,
)
from vidgen.contracts.shot_workflow import ShotWorkflowStatus
from vidgen.db.control_command_models import ControlCommandRecord
from vidgen.db.control_command_repository import (
    DEFAULT_LEASE_SECONDS,
    ControlCommandRepository,
)
from vidgen.review.workflow_control import WorkflowController

_LOGGER = logging.getLogger("vidgen.control_dispatcher")

#: Project statuses that mean the run stopped and nobody is expected to act.
#: A status that pairs with a waiting reason is handled before this set is
#: consulted, so a review-required stop is never mistaken for a failure.
TERMINAL_PROJECT_FAILURES = frozenset(
    {
        "render_failed",
        "render_cancelled",
        "FINAL_QA_FAILED",
        "shot_generation_failed",
        ReferenceWorkflowStatus.FAILED.value,
        ReferenceWorkflowStatus.CANCELLED.value,
    }
)


def default_dispatcher_id() -> str:
    """Stable within a process, unique across them - the same rule T17b uses."""
    configured = os.getenv("VIDGEN_CONTROL_DISPATCHER_ID")
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"[:128]


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """What one pass did. Counts only, so it is safe to log verbatim."""

    claimed: int = 0
    dispatched: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0


class ControlCommandDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        controller: WorkflowController,
        *,
        image_provider_name: str = "fake",
        image_model: str = "gpt-image-1",
        video_provider_name: str = "fake",
        visual_capability_profile: str = "default",
        dispatcher_id: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_size: int = 8,
    ) -> None:
        self._sessions = session_factory
        self._controller = controller
        self._image_provider_name = image_provider_name
        self._image_model = image_model
        self._video_provider_name = video_provider_name
        self._visual_capability_profile = visual_capability_profile
        self._id = dispatcher_id or default_dispatcher_id()
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size

    @property
    def dispatcher_id(self) -> str:
        return self._id

    def _context(self, session: Session) -> DispatchContext:
        return DispatchContext(
            session=session,
            controller=self._controller,
            image_provider_name=self._image_provider_name,
            image_model=self._image_model,
            video_provider_name=self._video_provider_name,
            visual_capability_profile=self._visual_capability_profile,
        )

    # -- one pass ---------------------------------------------------------
    def run_once(self) -> DispatchReport:
        """Dispatch a bounded batch of pending commands, then settle running ones."""
        claimed = dispatched = failed = skipped = 0
        with self._sessions() as session:
            repository = ControlCommandRepository(session)
            for record in repository.claimable(limit=self._batch_size):
                if not repository.claim(
                    record, claim_owner=self._id, lease_seconds=self._lease_seconds
                ):
                    # Another replica won the race, or the row moved on. Both are
                    # normal and both mean: do nothing to this command.
                    skipped += 1
                    continue
                session.commit()
                claimed += 1
                if self._dispatch_one(session, repository, record):
                    dispatched += 1
                else:
                    failed += 1
                session.commit()
        completed = self.settle_running()
        return DispatchReport(
            claimed=claimed,
            dispatched=dispatched,
            completed=completed,
            failed=failed,
            skipped=skipped,
        )

    def _dispatch_one(
        self,
        session: Session,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
    ) -> bool:
        repository.mark_dispatching(record)
        session.flush()
        try:
            outcome = dispatch(self._context(session), record)
        except DispatchFailure as failure:
            session.rollback()
            fresh = repository.get(record.project_id, record.id)
            if fresh is not None:
                repository.fail(
                    fresh,
                    ControlCommandFailure(
                        code=failure.code,
                        summary=failure.summary,
                        retryable=failure.retryable,
                        attempt=fresh.attempt,
                    ),
                )
            _LOGGER.warning(
                "control command failed",
                extra={"commandId": str(record.id), "code": failure.code},
            )
            return False
        except Exception:
            # An unexpected error is retryable within the command's bound: the
            # command stays durable and the next pass tries again rather than
            # leaving an accepted command with nothing behind it.
            session.rollback()
            fresh = repository.get(record.project_id, record.id)
            if fresh is not None:
                repository.fail(
                    fresh,
                    ControlCommandFailure(
                        code="command_dispatch_error",
                        summary="The dispatcher could not start this command's workflow.",
                        retryable=True,
                        attempt=fresh.attempt,
                    ),
                )
            _LOGGER.exception(
                "control command dispatch raised", extra={"commandId": str(record.id)}
            )
            return False
        if outcome.workflow_id is None:
            # A handler with no workflow to wait on produced its result
            # directly. It is still durable work: the resource it created is
            # committed in this same transaction.
            repository.complete(record, outcome.result or ControlCommandResult())
            return True
        repository.mark_running(
            record,
            workflow_id=outcome.workflow_id,
            run_id=outcome.run_id,
            progress=ControlCommandProgress(phase="running", percent=10),
        )
        if outcome.result is not None:
            record.result_type = (
                outcome.result.result_type.value if outcome.result.result_type else None
            )
            record.result_id = outcome.result.result_id
            record.result_summary = dict(outcome.result.summary)
            session.flush()
        return True

    # -- settling ---------------------------------------------------------
    def settle_running(self) -> int:
        """Move running commands to a terminal state from durable workflow state.

        Everything read here comes from a workflow query or from the rows the
        workflow itself wrote, so a command's status can never claim more than
        the workflow actually achieved.
        """
        settled = 0
        with self._sessions() as session:
            repository = ControlCommandRepository(session)
            for record in session.query(ControlCommandRecord).filter(
                ControlCommandRecord.status.in_(
                    [
                        ControlCommandStatus.RUNNING.value,
                        ControlCommandStatus.AWAITING_REVIEW.value,
                    ]
                )
            ):
                if self._settle_one(session, repository, record):
                    settled += 1
            session.commit()
        return settled

    def _settle_one(
        self,
        session: Session,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
    ) -> bool:
        command_type = ControlCommandType(record.command_type)
        workflow_id = record.workflow_id or ""
        if command_type in {
            ControlCommandType.REFERENCE_BUILD,
            ControlCommandType.REFERENCE_GENERATE,
            ControlCommandType.REFERENCE_APPLY,
        }:
            return self._settle_references(repository, record, workflow_id)
        if command_type is ControlCommandType.FINAL_QA_RUN:
            return self._settle_final_qa(repository, record, workflow_id)
        if command_type in {
            ControlCommandType.RENDER_RERENDER,
            ControlCommandType.FINAL_QA_REMEDIATION,
        }:
            return self._settle_render(repository, record, workflow_id)
        if command_type in {
            ControlCommandType.SHOT_REGENERATE,
            ControlCommandType.SHOT_RETRY,
            ControlCommandType.SHOT_REVIEW_CONTINUE,
        }:
            return self._settle_shot(repository, record, workflow_id)
        return self._settle_generation_run(session, repository, record)

    def _settle_references(
        self,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
        workflow_id: str,
    ) -> bool:
        status = self._controller.describe_references(workflow_id)
        if status is None:
            return False
        if status is ReferenceWorkflowStatus.AWAITING_APPROVAL:
            if ControlCommandStatus(record.status) is not ControlCommandStatus.AWAITING_REVIEW:
                repository.mark_awaiting_review(record, reason="reference_approval_required")
            return False
        if status is ReferenceWorkflowStatus.COMPLETE:
            return repository.complete(
                record,
                ControlCommandResult(
                    result_type=record.result_type,  # type: ignore[arg-type]
                    result_id=record.result_id,
                    summary={"references": status.value},
                ),
            )
        if status in {ReferenceWorkflowStatus.FAILED, ReferenceWorkflowStatus.CANCELLED}:
            return repository.fail(
                record,
                ControlCommandFailure(
                    code=status.value, summary="The continuity workflow did not bind references."
                ),
            )
        if ControlCommandStatus(record.status) is ControlCommandStatus.AWAITING_REVIEW:
            # The approval landed and binding resumed. Report running again so
            # the UI stops showing a review prompt nobody owes any more.
            repository.mark_running(
                record,
                workflow_id=workflow_id,
                run_id=record.run_id,
                progress=ControlCommandProgress(phase=status.value, percent=60),
            )
        return False

    def _settle_final_qa(
        self,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
        workflow_id: str,
    ) -> bool:
        result = self._controller.describe_final_qa(workflow_id)
        if result is None:
            return False
        if result.decision is None:
            return False
        summary = {
            "decision": result.decision,
            "status": result.status,
            "blocking_findings": str(result.blocking_finding_count),
            "review_findings": str(result.review_finding_count),
        }
        return repository.complete(
            record,
            ControlCommandResult(
                result_type=record.result_type,  # type: ignore[arg-type]
                result_id=record.result_id,
                summary=summary,
            ),
        )

    def _settle_render(
        self,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
        workflow_id: str,
    ) -> bool:
        result = self._controller.describe_render(workflow_id)
        if result is None:
            return False
        if result.status == "render_complete":
            return repository.complete(
                record,
                ControlCommandResult(
                    result_type=record.result_type,  # type: ignore[arg-type]
                    result_id=result.render_job_id,
                    summary={"render_status": result.status, "reused": str(result.reused).lower()},
                ),
            )
        if result.status in {"render_failed", "render_cancelled"}:
            return repository.fail(
                record,
                ControlCommandFailure(
                    code=result.error_code or result.status,
                    summary="The render did not complete.",
                ),
            )
        repository.mark_progress(
            record,
            ControlCommandProgress(phase=result.status, percent=min(99, result.progress_percent)),
        )
        return False

    def _settle_shot(
        self,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
        workflow_id: str,
    ) -> bool:
        progress = self._controller.describe_shot_by_id(workflow_id)
        if progress is None:
            return False
        if progress.state is ShotWorkflowStatus.LOCKED:
            return repository.complete(
                record,
                ControlCommandResult(
                    result_type=record.result_type,  # type: ignore[arg-type]
                    result_id=record.result_id,
                    summary={"shot_state": progress.state.value},
                ),
            )
        if progress.state is ShotWorkflowStatus.HUMAN_REVIEW_REQUIRED:
            if ControlCommandStatus(record.status) is not ControlCommandStatus.AWAITING_REVIEW:
                repository.mark_awaiting_review(record, reason="shot_review_required")
            return False
        if progress.state in {ShotWorkflowStatus.FAILED, ShotWorkflowStatus.CANCELLED}:
            return repository.fail(
                record,
                ControlCommandFailure(
                    code=progress.state.value,
                    summary="The replacement shot workflow did not lock an output.",
                    retryable=bool(progress.retryable),
                ),
            )
        repository.mark_progress(
            record, ControlCommandProgress(phase=progress.current_stage, percent=50)
        )
        return False

    def _settle_generation_run(
        self,
        session: Session,
        repository: ControlCommandRepository,
        record: ControlCommandRecord,
    ) -> bool:
        """A revision or continuation is settled by its project workflow's state."""
        state = self._controller.describe_project(record.workflow_id or "")
        if state is None:
            return False
        runs = GenerationRunService(session)
        run = runs.active(record.project_id)
        if state.status in {"completed", "final_qa_passed"}:
            if run is not None and run.origin_command_id == record.id:
                runs.settle(run, ProjectGenerationRunStatus.COMPLETED)
            return repository.complete(
                record,
                ControlCommandResult(
                    result_type=record.result_type,  # type: ignore[arg-type]
                    result_id=record.result_id,
                    summary={"project_status": state.status},
                ),
            )
        if state.waiting_reason:
            if ControlCommandStatus(record.status) is not ControlCommandStatus.AWAITING_REVIEW:
                repository.mark_awaiting_review(record, reason=state.waiting_reason)
            if run is not None and run.origin_command_id == record.id:
                runs.settle(run, ProjectGenerationRunStatus.AWAITING_REVIEW)
            return False
        if state.cancelled:
            if run is not None and run.origin_command_id == record.id:
                runs.settle(run, ProjectGenerationRunStatus.CANCELLED)
            return repository.fail(
                record,
                ControlCommandFailure(
                    code="project_cancelled", summary="The project workflow was cancelled."
                ),
            )
        if state.status in TERMINAL_PROJECT_FAILURES:
            # The run stopped and is not waiting on anybody. Leaving the command
            # "running" here would strand it - and its generation run - forever,
            # which is the exact failure mode this task exists to remove.
            if run is not None and run.origin_command_id == record.id:
                runs.settle(run, ProjectGenerationRunStatus.FAILED)
            return repository.fail(
                record,
                ControlCommandFailure(
                    code=state.status[:128],
                    summary="The project workflow stopped without completing.",
                ),
            )
        repository.mark_progress(
            record, ControlCommandProgress(phase=state.status[:64], percent=50)
        )
        return False

    # -- long-running loop -------------------------------------------------
    def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        should_stop: object = None,
        max_passes: int | None = None,
    ) -> None:
        """Poll until asked to stop. ``should_stop`` is any callable returning bool."""
        passes = 0
        while True:
            if callable(should_stop) and should_stop():
                _LOGGER.info("control dispatcher stopping")
                return
            report = self.run_once()
            passes += 1
            if report.claimed or report.completed:
                _LOGGER.info(
                    "control dispatcher pass",
                    extra={
                        "claimed": report.claimed,
                        "dispatched": report.dispatched,
                        "completed": report.completed,
                        "failed": report.failed,
                    },
                )
            if max_passes is not None and passes >= max_passes:
                return
            time.sleep(poll_seconds)


def command_status(session: Session, project_id: UUID, command_id: UUID) -> ControlCommand | None:
    """Read one command's owner-facing projection. Used by the CLI and tests."""
    record = ControlCommandRepository(session).get(project_id, command_id)
    return command_projection(record) if record is not None else None
