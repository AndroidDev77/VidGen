"""Owner-scoped workflow control for the review UI.

The API never runs pipeline work itself: it resolves a stable workflow ID,
records the run, and asks a :class:`WorkflowController` to start, cancel, query,
or command a workflow. Only compact identifiers, hashes, statuses and counts
cross this boundary, so no source bytes, transcript or script text, images,
videos, render manifests or provider payloads ever enter a Temporal message.

:class:`FakeWorkflowController` gives API and frontend tests a deterministic
implementation, so Temporal is not required to exercise T18.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from vidgen.contracts.continuity_workflow import (
    ReferenceApprovalSignal,
    ReferenceWorkflowInput,
    ReferenceWorkflowStatus,
)
from vidgen.contracts.publication import PublicationActivityInput, PublicationActivityResult
from vidgen.contracts.shot_workflow import (
    ShotWorkflowCommand,
    ShotWorkflowCommandResult,
    ShotWorkflowInput,
    ShotWorkflowProgress,
    ShotWorkflowStatus,
)
from vidgen.contracts.workflow import (
    FinalQAActivityInput,
    FinalQAActivityResult,
    ProjectWorkflowInput,
    ProjectWorkflowState,
    RenderActivityInput,
    RenderActivityResult,
)

TASK_QUEUE = "vidgen-projects"
#: T25 uploads run on their own queue so a multi-hour video cannot starve the
#: ordinary project activities.
PUBLISHER_TASK_QUEUE = "vidgen-publisher"

#: How long a single Temporal RPC may take before the controller gives up on it.
#: Every call here is made from a synchronous caller - an API request thread or a
#: dispatcher pass - so an RPC with no deadline is an unbounded hang, not a slow
#: answer. A query against a parked shot workflow that a worker cannot schedule
#: is exactly that case.
DEFAULT_RPC_TIMEOUT_SECONDS = 10.0

#: Headroom above the RPC deadline for the surrounding work - connecting the
#: first time, and Temporal's own retries inside one call. The outer guard only
#: exists so a wedged connection cannot block a caller forever; the RPC deadline
#: is what normally ends a slow call.
CALL_TIMEOUT_MARGIN_SECONDS = 20.0


class WorkflowControlUnavailable(RuntimeError):
    """The cluster could not be asked. Never a verdict about the workflow.

    This is the distinction the control plane has to keep: "there is no such
    workflow" is an answer, and "nobody answered" is not. Returning ``None`` for
    the second would make a caller start a replacement child for one that is
    alive, so the failure is raised as its own type instead - and the dispatcher
    treats it as an infrastructure hiccup to wait out rather than as a command
    that has used up an attempt.
    """

    def __init__(self, summary: str, *, workflow_id: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.workflow_id = workflow_id


#: gRPC statuses that describe the *call*, not the workflow: the cluster was
#: unreachable, overloaded, or too slow. None of them is evidence about what the
#: caller asked for, so all of them become :class:`WorkflowControlUnavailable`.
#: ``NOT_FOUND`` is deliberately absent - it is an answer, and callers that can
#: act on it must keep seeing it as one.
_TRANSIENT_RPC_STATUSES = frozenset(
    {
        "CANCELLED",
        "UNKNOWN",
        "DEADLINE_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "ABORTED",
        "INTERNAL",
        "UNAVAILABLE",
    }
)


def project_workflow_id(project_id: UUID) -> str:
    """Return the stable per-project workflow ID a retried start reuses."""
    return f"vidgen-project-{project_id}"


def reference_workflow_id(reference_run_id: UUID) -> str:
    """The stable per-reference-run T19 workflow ID, shared with the parent.

    Keyed by the reference run rather than the project: a project drafts a new
    reference run whenever its authoritative storyboard changes, and each is its
    own durable approval pause.
    """
    return f"vidgen-references-{reference_run_id}"


def publication_workflow_id(publication_run_id: UUID) -> str:
    """The stable per-publication workflow ID. A retried start adopts it.

    Keyed by the publication run rather than the project: a project may publish
    more than one render over its life, and each is its own workflow.
    """
    return f"vidgen-publication-{publication_run_id}"


class WorkflowController(Protocol):
    """The narrow control surface the T18 API depends on."""

    def start_project(self, request: ProjectWorkflowInput) -> tuple[str, str]:
        """Start (or adopt) the project workflow and return ``(workflow_id, run_id)``."""

    def cancel_project(self, workflow_id: str) -> None: ...

    def describe_project(self, workflow_id: str) -> ProjectWorkflowState | None: ...

    def project_execution_status(self, workflow_id: str) -> str | None:
        """The cluster's own view of the execution, independent of its query.

        ``describe_project`` asks the *workflow* what it thinks; a workflow that
        has already failed answers nothing at all. This reports the execution
        status the cluster records - ``running``, ``completed``, ``failed``,
        ``cancelled``, ``terminated`` or ``timed_out`` - so a
        caller can tell "still working" from "died an hour ago". ``None`` means
        the cluster could not be asked, which is never evidence of a failure.
        """

    def send_shot_command(
        self, workflow_id: str, command: ShotWorkflowCommand
    ) -> ShotWorkflowCommandResult: ...

    def describe_shot(self, workflow_id: str) -> ShotWorkflowProgress | None: ...

    def start_publication(self, request: PublicationActivityInput) -> tuple[str, str]:
        """Start (or adopt) the T25 publication workflow on the publisher queue."""

    def cancel_publication(self, workflow_id: str) -> None: ...

    def describe_publication(self, workflow_id: str) -> PublicationActivityResult | None: ...

    # -- T18b durable control-command dispatch targets ---------------------
    # Each of these starts or signals a *real* workflow and returns its actual
    # identity. Nothing below may return a calculated ID: the control command
    # only becomes ``running`` once one of these has succeeded.

    def start_references(self, request: ReferenceWorkflowInput) -> tuple[str, str]:
        """Start (or adopt) the T19 workflow for one reference run."""

    def signal_reference_approval(self, workflow_id: str, signal: ReferenceApprovalSignal) -> bool:
        """Deliver an approval to the waiting T19 workflow. ``False`` if absent."""

    def describe_references(self, workflow_id: str) -> ReferenceWorkflowStatus | None: ...

    def start_shot(self, request: ShotWorkflowInput) -> tuple[str, str]:
        """Start (or adopt) a replacement T16 child for exactly one shot."""

    def describe_shot_by_id(self, workflow_id: str) -> ShotWorkflowProgress | None: ...

    def start_final_qa(self, request: FinalQAActivityInput, workflow_id: str) -> tuple[str, str]:
        """Start (or adopt) a manual T22 run against the current render."""

    def describe_final_qa(self, workflow_id: str) -> FinalQAActivityResult | None: ...

    def start_render(self, request: RenderActivityInput, workflow_id: str) -> tuple[str, str]:
        """Start (or adopt) a T17b render through the canonical executor."""

    def describe_render(self, workflow_id: str) -> RenderActivityResult | None: ...

    def cancel_workflow(self, workflow_id: str) -> bool:
        """Cancel any dispatched workflow by ID. ``False`` if it is already gone."""


#: Project workflow statuses after which no execution is still running.
_CLOSED_PROJECT = {"completed", "final_qa_passed", "cancelled"}

#: Execution statuses a controller may report, mirroring Temporal's
#: ``WorkflowExecutionStatus`` with its spellings normalised.
EXECUTION_RUNNING = "running"
EXECUTION_COMPLETED = "completed"
EXECUTION_FAILED = "failed"
EXECUTION_CANCELLED = "cancelled"
EXECUTION_TERMINATED = "terminated"
EXECUTION_TIMED_OUT = "timed_out"

#: Execution statuses after which nothing is running any more. A project whose
#: database row still says ``running`` while the cluster reports one of these is
#: stale, and every continuation it blocks is blocked for no reason.
TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        EXECUTION_COMPLETED,
        EXECUTION_FAILED,
        EXECUTION_CANCELLED,
        EXECUTION_TERMINATED,
        EXECUTION_TIMED_OUT,
    }
)

#: ``WorkflowExecutionStatus`` value -> the spelling above. Temporal numbers the
#: enum from 1 (``RUNNING``); a value outside this table is reported verbatim in
#: lower case rather than guessed at.
_TEMPORAL_EXECUTION_STATUSES: dict[int, str] = {
    1: EXECUTION_RUNNING,
    2: EXECUTION_COMPLETED,
    3: EXECUTION_FAILED,
    4: EXECUTION_CANCELLED,
    5: EXECUTION_TERMINATED,
    6: EXECUTION_RUNNING,  # CONTINUED_AS_NEW: a successor execution is live.
    7: EXECUTION_TIMED_OUT,
}


class FakeWorkflowController:
    """Deterministic in-memory controller used by tests and local development."""

    def __init__(self) -> None:
        self.started: dict[str, ProjectWorkflowInput] = {}
        self.states: dict[str, ProjectWorkflowState] = {}
        self.cancelled: list[str] = []
        self.shot_commands: list[tuple[str, ShotWorkflowCommand]] = []
        self.shot_states: dict[str, ShotWorkflowProgress] = {}
        self.start_calls = 0
        self.publications: dict[str, PublicationActivityInput] = {}
        self.publication_states: dict[str, PublicationActivityResult] = {}
        self.cancelled_publications: list[str] = []
        self.publication_start_calls = 0
        self.references: dict[str, ReferenceWorkflowInput] = {}
        self.reference_statuses: dict[str, ReferenceWorkflowStatus] = {}
        self.reference_approvals: list[tuple[str, ReferenceApprovalSignal]] = []
        self.reference_start_calls = 0
        self.shots: dict[str, ShotWorkflowInput] = {}
        self.shot_start_calls = 0
        self.final_qa: dict[str, FinalQAActivityInput] = {}
        self.final_qa_states: dict[str, FinalQAActivityResult] = {}
        self.renders: dict[str, RenderActivityInput] = {}
        self.render_states: dict[str, RenderActivityResult] = {}
        self.cancelled_workflows: list[str] = []
        #: How many executions each project workflow ID has had, and the run ID
        #: of the latest one. A continuation starts a new execution.
        self.project_executions: dict[str, int] = {}
        self.project_run_ids: dict[str, str] = {}
        #: Workflow IDs the fake cluster reports as already gone.
        self.missing_workflows: set[str] = set()
        #: Workflow IDs the fake cluster cannot answer for at all. This is the
        #: other half of ``missing_workflows`` and the distinction the control
        #: plane turns on: "gone" is an answer a caller may act on, "could not
        #: be asked" is not, and a test needs to reproduce the second without a
        #: cluster to break.
        self.unavailable_workflows: set[str] = set()
        #: Workflow IDs whose cancellation the fake cluster cannot accept, and
        #: whose approval signal it cannot accept. Separate from the set above
        #: because each of these is its own "answering nothing is not an answer"
        #: decision: a swallowed cancel reports a stop that never happened, and
        #: a swallowed signal buys a second paid reference run.
        self.cancel_failures: set[str] = set()
        self.signal_failures: set[str] = set()
        #: Execution statuses the fake cluster reports, overriding what the
        #: workflow's own state implies. This is how a test reproduces the case
        #: the reconciler exists for: an execution that died while the
        #: database still believes it is running.
        self.execution_statuses: dict[str, str] = {}

    def start_project(self, request: ProjectWorkflowInput) -> tuple[str, str]:
        """Adopt a live execution, or start a new one under the same ID.

        This mirrors ``ALLOW_DUPLICATE``: a project workflow that has stopped -
        cancelled, completed, or paused for a human - is continued by starting a
        *new* execution with a new run ID, not by signalling the closed one.
        """
        workflow_id = project_workflow_id(request.project_id)
        self.start_calls += 1
        state = self.states.get(workflow_id)
        live = state is not None and not state.cancelled and state.status not in _CLOSED_PROJECT
        if workflow_id in self.started and live:
            return workflow_id, self.project_run_ids[workflow_id]
        executions = self.project_executions.get(workflow_id, 0) + 1
        self.project_executions[workflow_id] = executions
        run_id = f"{workflow_id}-run" if executions == 1 else f"{workflow_id}-run-{executions}"
        self.project_run_ids[workflow_id] = run_id
        self.started[workflow_id] = request
        self.states[workflow_id] = ProjectWorkflowState(
            project_id=request.project_id, status="ingesting"
        )
        return workflow_id, run_id

    def cancel_project(self, workflow_id: str) -> None:
        self.cancelled.append(workflow_id)
        state = self.states.get(workflow_id)
        if state is not None:
            self.states[workflow_id] = state.model_copy(
                update={"status": "cancelled", "cancelled": True}
            )

    def describe_project(self, workflow_id: str) -> ProjectWorkflowState | None:
        return self.states.get(workflow_id)

    def project_execution_status(self, workflow_id: str) -> str | None:
        override = self.execution_statuses.get(workflow_id)
        if override is not None:
            return override
        if workflow_id in self.missing_workflows:
            return None
        state = self.states.get(workflow_id)
        if state is None:
            return None
        if state.cancelled:
            return EXECUTION_CANCELLED
        if state.status in _CLOSED_PROJECT:
            return EXECUTION_COMPLETED
        return EXECUTION_RUNNING

    def send_shot_command(
        self, workflow_id: str, command: ShotWorkflowCommand
    ) -> ShotWorkflowCommandResult:
        self.shot_commands.append((workflow_id, command))
        progress = self.shot_states.get(workflow_id)
        state = progress.state if progress else ShotWorkflowStatus.DEFINED
        if command.command in {"retry", "resume"} and not (
            progress is not None and progress.retryable
        ):
            return ShotWorkflowCommandResult(
                command_id=command.command_id,
                accepted=False,
                state=state,
                code="shot_not_retryable",
            )
        code = {
            "cancel": "accepted",
            "regenerate": "start_new_child_identity",
            "retry": "retry_scheduled",
            "resume": "retry_scheduled",
        }.get(command.command, "accepted")
        return ShotWorkflowCommandResult(
            command_id=command.command_id, accepted=True, state=state, code=code
        )

    def describe_shot(self, workflow_id: str) -> ShotWorkflowProgress | None:
        return self.shot_states.get(workflow_id)

    def start_publication(self, request: PublicationActivityInput) -> tuple[str, str]:
        workflow_id = publication_workflow_id(request.publication_run_id)
        self.publication_start_calls += 1
        # Adopt rather than duplicate: a repeated start must never produce a
        # second workflow driving the same upload.
        self.publications.setdefault(workflow_id, request)
        return workflow_id, f"{workflow_id}-run"

    def cancel_publication(self, workflow_id: str) -> None:
        self.cancelled_publications.append(workflow_id)

    def describe_publication(self, workflow_id: str) -> PublicationActivityResult | None:
        return self.publication_states.get(workflow_id)

    # -- T18b dispatch targets --------------------------------------------
    def start_references(self, request: ReferenceWorkflowInput) -> tuple[str, str]:
        workflow_id = reference_workflow_id(request.reference_run_id)
        self.reference_start_calls += 1
        if self.references.setdefault(workflow_id, request) is request:
            self.reference_statuses[workflow_id] = ReferenceWorkflowStatus.AWAITING_APPROVAL
        return workflow_id, f"{workflow_id}-run"

    def signal_reference_approval(self, workflow_id: str, signal: ReferenceApprovalSignal) -> bool:
        if workflow_id in self.signal_failures:
            raise WorkflowControlUnavailable(
                "The workflow service could not be reached (unavailable).",
                workflow_id=workflow_id,
            )
        if workflow_id not in self.references:
            return False
        self.reference_approvals.append((workflow_id, signal))
        self.reference_statuses[workflow_id] = ReferenceWorkflowStatus.BINDING
        return True

    def describe_references(self, workflow_id: str) -> ReferenceWorkflowStatus | None:
        return self.reference_statuses.get(workflow_id)

    def start_shot(self, request: ShotWorkflowInput) -> tuple[str, str]:
        from packages.workflows.shot_policy import temporal_shot_workflow_id

        workflow_id = temporal_shot_workflow_id(request.workflow_identity)
        self.shot_start_calls += 1
        self.shots.setdefault(workflow_id, request)
        return workflow_id, f"{workflow_id}-run"

    def describe_shot_by_id(self, workflow_id: str) -> ShotWorkflowProgress | None:
        if workflow_id in self.unavailable_workflows:
            raise WorkflowControlUnavailable(
                "The workflow service could not be reached (unavailable).",
                workflow_id=workflow_id,
            )
        return self.shot_states.get(workflow_id)

    def start_final_qa(self, request: FinalQAActivityInput, workflow_id: str) -> tuple[str, str]:
        self.final_qa.setdefault(workflow_id, request)
        return workflow_id, f"{workflow_id}-run"

    def describe_final_qa(self, workflow_id: str) -> FinalQAActivityResult | None:
        return self.final_qa_states.get(workflow_id)

    def start_render(self, request: RenderActivityInput, workflow_id: str) -> tuple[str, str]:
        self.renders.setdefault(workflow_id, request)
        return workflow_id, f"{workflow_id}-run"

    def describe_render(self, workflow_id: str) -> RenderActivityResult | None:
        return self.render_states.get(workflow_id)

    def cancel_workflow(self, workflow_id: str) -> bool:
        if workflow_id in self.cancel_failures:
            raise WorkflowControlUnavailable(
                "The workflow service could not be reached (unavailable).",
                workflow_id=workflow_id,
            )
        self.cancelled_workflows.append(workflow_id)
        return workflow_id not in self.missing_workflows


class _ControllerLoop:
    """One event loop, owned by one daemon thread, shared by every call.

    ``asyncio.run`` per call is why a query used to pay for a fresh
    ``Client.connect``: a Temporal client is bound to the loop that created it,
    so the only way to reuse the client is to reuse the loop. A single daemon
    thread owns that loop for the life of the process, and each controller
    method submits its coroutine to it and blocks on the result - so callers
    stay exactly as synchronous as they were, and the connection setup happens
    once instead of on every describe.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="vidgen-workflow-control", daemon=True
                ).start()
                self._loop = loop
            return loop

    def run(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float) -> object:
        """Run one coroutine on the shared loop and wait ``timeout`` for it."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._ensure())
        try:
            return future.result(timeout)
        except FutureTimeoutError as expired:
            # The RPC deadline should already have ended the call. Reaching here
            # means the connection itself is wedged, so abandon it rather than
            # holding the caller - a request thread or a dispatcher pass - open.
            future.cancel()
            raise WorkflowControlUnavailable(
                "The workflow service did not respond within the call deadline."
            ) from expired


class TemporalWorkflowController:
    """Adapter over the existing Temporal client and parent project workflow."""

    def __init__(
        self,
        target_host: str,
        namespace: str = "default",
        *,
        api_key: str | None = None,
        tls_enabled: bool | None = None,
        rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    ) -> None:
        self._target_host = target_host
        self._namespace = namespace
        self._api_key = api_key
        # Temporal Cloud is always TLS. Defaulting to "TLS whenever an API key
        # is configured" means a deployed environment cannot accidentally
        # connect in plaintext, while a local dev server still works.
        self._tls_enabled = tls_enabled if tls_enabled is not None else api_key is not None
        self._rpc_timeout = timedelta(seconds=rpc_timeout_seconds)
        self._call_timeout = rpc_timeout_seconds + CALL_TIMEOUT_MARGIN_SECONDS
        self._loop = _ControllerLoop()
        #: The one connected client, created on the controller's own loop. It is
        #: never rebuilt per call: the Temporal client owns a connection that
        #: reconnects on its own, and paying for a handshake before every query
        #: is what made a busy worker look like an unreachable one.
        self._connection: object | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._pid = os.getpid()

    def _rebuild_if_forked(self) -> None:
        """Start over in a child process. Nothing survives a fork intact.

        A worker that forks after a controller has been used inherits an event
        loop with no thread running it and a client bound to that loop. Calls
        would then hang until the outer deadline in a process that never
        connected. Rebuilding on first use in the child is cheap and makes the
        controller safe wherever the deployment chooses to fork.
        """
        pid = os.getpid()
        if self._pid == pid:
            return
        self._pid = pid
        self._loop = _ControllerLoop()
        self._connection = None
        self._connect_lock = None

    def _run(self, coroutine: object) -> object:
        """Run ``coroutine`` on the shared loop, classifying transport failures.

        A gRPC status that describes the call rather than the workflow becomes
        :class:`WorkflowControlUnavailable` here, once, so no caller has to know
        about ``temporalio`` to tell "the cluster did not answer" from "the
        cluster answered no". ``NOT_FOUND`` and the other definitive statuses
        pass through untouched, because callers act on them.
        """
        from temporalio.service import RPCError

        self._rebuild_if_forked()
        try:
            return self._loop.run(
                coroutine,  # type: ignore[arg-type]
                timeout=self._call_timeout,
            )
        except RPCError as error:
            if getattr(error.status, "name", "") not in _TRANSIENT_RPC_STATUSES:
                raise
            raise WorkflowControlUnavailable(
                f"The workflow service could not be reached ({error.status.name.lower()})."
            ) from error
        except OSError as error:
            raise WorkflowControlUnavailable(
                "The workflow service could not be reached."
            ) from error

    async def _client(self) -> object:
        from temporalio.client import Client, TLSConfig

        if self._connect_lock is None:
            # Created on the controller's loop, and only ever awaited there, so
            # this lazy build cannot race: there is exactly one loop thread.
            self._connect_lock = asyncio.Lock()
        if self._connection is not None:
            return self._connection
        async with self._connect_lock:
            if self._connection is None:
                self._connection = await Client.connect(
                    self._target_host,
                    namespace=self._namespace,
                    api_key=self._api_key,
                    tls=TLSConfig() if self._tls_enabled else False,
                )
        return self._connection

    def start_project(self, request: ProjectWorkflowInput) -> tuple[str, str]:
        """Start the project's generation run, adopting a live execution.

        Not ``ALLOW_DUPLICATE_FAILED_ONLY``: since T18b a project workflow
        *completes* at every human pause - references awaiting approval, shots
        awaiting review, final QA review required - so continuing a project is a
        new execution of a workflow that closed successfully, not a retry of a
        failed one. Each execution carries its own immutable generation run, and
        an execution that is still running is adopted rather than duplicated.
        """
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.project import ProjectWorkflow

        workflow_id = project_workflow_id(request.project_id)

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    ProjectWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def cancel_project(self, workflow_id: str) -> None:
        from packages.workflows.project import ProjectWorkflow

        async def run() -> None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            await handle.signal(ProjectWorkflow.cancel_project, rpc_timeout=self._rpc_timeout)

        self._run(run())

    def describe_project(self, workflow_id: str) -> ProjectWorkflowState | None:
        from temporalio.service import RPCError

        from packages.workflows.project import ProjectWorkflow

        async def run() -> ProjectWorkflowState | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                state = await handle.query(
                    ProjectWorkflow.project_state, rpc_timeout=self._rpc_timeout
                )
            except RPCError:
                # Query can fail transiently when no worker is currently polling
                # (e.g. immediately after a worker restart). Return None so the
                # dispatcher skips this command and retries on the next pass.
                return None
            return state if isinstance(state, ProjectWorkflowState) else None

        result = self._run(run())
        return result if isinstance(result, ProjectWorkflowState) else None

    def project_execution_status(self, workflow_id: str) -> str | None:
        """Ask the cluster, not the workflow, whether the execution is alive.

        ``describe`` answers for an execution that already failed, which a query
        cannot: the query needs a worker and a workflow willing to run it. That
        difference is the whole point - a project whose workflow died is exactly
        the project whose row still says ``running``.
        """
        from temporalio.service import RPCError

        async def run() -> str | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                description = await handle.describe(rpc_timeout=self._rpc_timeout)
            except RPCError:
                # Not found, or the cluster is unreachable. Either way this is
                # not evidence that the execution stopped, so say nothing.
                return None
            status = getattr(description, "status", None)
            if status is None:
                return None
            value = getattr(status, "value", None)
            if isinstance(value, int) and value in _TEMPORAL_EXECUTION_STATUSES:
                return _TEMPORAL_EXECUTION_STATUSES[value]
            name = getattr(status, "name", None)
            return str(name).lower() if name else None

        result = self._run(run())
        return result if isinstance(result, str) else None

    def send_shot_command(
        self, workflow_id: str, command: ShotWorkflowCommand
    ) -> ShotWorkflowCommandResult:
        from packages.workflows.shot import ShotWorkflow

        async def run() -> ShotWorkflowCommandResult:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            await handle.signal(ShotWorkflow.command, command, rpc_timeout=self._rpc_timeout)
            result = await handle.query(
                ShotWorkflow.command_result, command.command_id, rpc_timeout=self._rpc_timeout
            )
            if isinstance(result, ShotWorkflowCommandResult):
                return result
            return ShotWorkflowCommandResult(
                command_id=command.command_id,
                accepted=True,
                state=ShotWorkflowStatus.DEFINED,
                code="accepted",
            )

        result = self._run(run())
        assert isinstance(result, ShotWorkflowCommandResult)
        return result

    def describe_shot(self, workflow_id: str) -> ShotWorkflowProgress | None:
        from packages.workflows.shot import ShotWorkflow

        async def run() -> ShotWorkflowProgress | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(ShotWorkflow.shot_state, rpc_timeout=self._rpc_timeout)
            return getattr(state, "progress", None)

        result = self._run(run())
        return result if isinstance(result, ShotWorkflowProgress) else None

    def start_publication(self, request: PublicationActivityInput) -> tuple[str, str]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.publication import YouTubePublicationWorkflow

        workflow_id = publication_workflow_id(request.publication_run_id)

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    YouTubePublicationWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=PUBLISHER_TASK_QUEUE,
                    # Not ALLOW_DUPLICATE_FAILED_ONLY: this workflow *completes*
                    # at every waiting state - quota blocked, reauthorization
                    # required, held for review - so a later resume of the same
                    # publication run is a new execution of a workflow that
                    # closed successfully, not a retry of a failed one.
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                # One is already running for this publication. Adopting it is
                # the right answer: the upload is durable and idempotent, and a
                # second execution would only race the first for the same
                # resumable session.
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def cancel_publication(self, workflow_id: str) -> None:
        from packages.workflows.publication import YouTubePublicationWorkflow

        async def run() -> None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            await handle.signal(
                YouTubePublicationWorkflow.cancel_publication, rpc_timeout=self._rpc_timeout
            )

        self._run(run())

    def describe_publication(self, workflow_id: str) -> PublicationActivityResult | None:
        from packages.workflows.publication import YouTubePublicationWorkflow

        async def run() -> PublicationActivityResult | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(
                YouTubePublicationWorkflow.state, rpc_timeout=self._rpc_timeout
            )
            return state if isinstance(state, PublicationActivityResult) else None

        result = self._run(run())
        return result if isinstance(result, PublicationActivityResult) else None

    # -- T18b dispatch targets ---------------------------------------------
    def start_references(self, request: ReferenceWorkflowInput) -> tuple[str, str]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.continuity import ContinuityReferenceWorkflow

        workflow_id = reference_workflow_id(request.reference_run_id)

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    ContinuityReferenceWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                # The project workflow already owns this reference run. Adopting
                # it is required, not merely convenient: a second execution
                # would draft the same sheets again and wait for its own
                # approval that the UI would never send.
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def signal_reference_approval(self, workflow_id: str, signal: ReferenceApprovalSignal) -> bool:
        """``False`` only when no workflow is waiting for this approval.

        The caller answers ``False`` by *starting* a reference workflow, which
        drafts sheets and spends. So only ``NOT_FOUND`` may say it: a cluster
        that merely failed to answer says nothing about whether one is waiting,
        and reading that as "there is none" buys a second run of paid work -
        the same duplicate-child harm ``describe_shot_by_id`` refuses.
        """
        from temporalio.service import RPCError, RPCStatusCode

        from packages.workflows.continuity import ContinuityReferenceWorkflow

        async def run() -> bool:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                await handle.signal(
                    ContinuityReferenceWorkflow.approve, signal, rpc_timeout=self._rpc_timeout
                )
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
                # No live workflow is waiting for this approval. The decision is
                # already persisted; the caller decides whether to start one.
                return False
            return True

        result = self._run(run())
        return bool(result)

    def describe_references(self, workflow_id: str) -> ReferenceWorkflowStatus | None:
        from packages.workflows.continuity import ContinuityReferenceWorkflow

        async def run() -> ReferenceWorkflowStatus | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(
                ContinuityReferenceWorkflow.status, rpc_timeout=self._rpc_timeout
            )
            return state if isinstance(state, ReferenceWorkflowStatus) else None

        result = self._run(run())
        return result if isinstance(result, ReferenceWorkflowStatus) else None

    def start_shot(self, request: ShotWorkflowInput) -> tuple[str, str]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.shot import ShotWorkflow
        from packages.workflows.shot_policy import temporal_shot_workflow_id

        workflow_id = temporal_shot_workflow_id(request.workflow_identity)

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    ShotWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                # A duplicated regeneration command resolves to the same
                # reproducible identity, so it must adopt the replacement child
                # rather than pay for a second one.
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def describe_shot_by_id(self, workflow_id: str) -> ShotWorkflowProgress | None:
        """``None`` only when the cluster says there is no such execution.

        A transient failure - no worker polling, an unreachable cluster, a query
        the worker could not schedule in time - is not evidence that the child is
        gone, and answering ``None`` would make the dispatcher pay for a
        replacement child that already exists. Those statuses have already become
        :class:`WorkflowControlUnavailable` in :meth:`_run`, which propagates so
        the caller waits for the cluster instead of duplicating a live child.
        """
        from temporalio.service import RPCError, RPCStatusCode

        try:
            return self.describe_shot(workflow_id)
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                # A definitive status that is still not an answer about the
                # child - a rejected credential, say. Never ``None``.
                raise
            # There is no such execution: the identity this ID was rebuilt from
            # never produced a child, or its history has expired. The caller
            # treats that as "nothing to resume" and starts a replacement.
            return None

    def start_final_qa(self, request: FinalQAActivityInput, workflow_id: str) -> tuple[str, str]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.control import FinalEditorialQAWorkflow

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    FinalEditorialQAWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def describe_final_qa(self, workflow_id: str) -> FinalQAActivityResult | None:
        from packages.workflows.control import FinalEditorialQAWorkflow

        async def run() -> FinalQAActivityResult | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(
                FinalEditorialQAWorkflow.final_qa_state, rpc_timeout=self._rpc_timeout
            )
            return state if isinstance(state, FinalQAActivityResult) else None

        result = self._run(run())
        return result if isinstance(result, FinalQAActivityResult) else None

    def start_render(self, request: RenderActivityInput, workflow_id: str) -> tuple[str, str]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from packages.workflows.control import RenderWorkflow

        async def run() -> tuple[str, str]:
            client = await self._client()
            try:
                handle = await client.start_workflow(  # type: ignore[attr-defined]
                    RenderWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=self._rpc_timeout,
                )
            except WorkflowAlreadyStartedError:
                existing = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
                return workflow_id, existing.first_execution_run_id or ""
            return workflow_id, handle.result_run_id or handle.first_execution_run_id or ""

        result = self._run(run())
        assert isinstance(result, tuple)
        return result

    def describe_render(self, workflow_id: str) -> RenderActivityResult | None:
        from packages.workflows.control import RenderWorkflow

        async def run() -> RenderActivityResult | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(RenderWorkflow.render_state, rpc_timeout=self._rpc_timeout)
            return state if isinstance(state, RenderActivityResult) else None

        result = self._run(run())
        return result if isinstance(result, RenderActivityResult) else None

    def cancel_workflow(self, workflow_id: str) -> bool:
        """Ask the cluster to cancel a dispatched workflow.

        A workflow that no longer exists is reported as ``False`` rather than
        raised: the command it belonged to is finished either way, and the
        dispatcher must still be able to settle the row.

        A cluster that could not be reached is *not* that. The caller marks the
        command ``cancelled`` regardless of what this returns, so swallowing a
        transient failure would report a stop that never happened and leave the
        workflow running and spending. Those propagate, and the next pass tries
        the cancellation again.
        """
        from temporalio.service import RPCError, RPCStatusCode

        #: Statuses that mean there is nothing left to cancel. Temporal answers
        #: ``NOT_FOUND`` both for an execution it has never heard of and for one
        #: that has already closed.
        finished = {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}

        async def run() -> bool:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                await handle.cancel(rpc_timeout=self._rpc_timeout)
            except RPCError as error:
                if error.status not in finished:
                    raise
                return False
            return True

        return bool(self._run(run()))
