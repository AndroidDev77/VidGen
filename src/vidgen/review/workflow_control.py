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

from typing import Protocol
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
        #: Workflow IDs the fake cluster reports as already gone.
        self.missing_workflows: set[str] = set()

    def start_project(self, request: ProjectWorkflowInput) -> tuple[str, str]:
        workflow_id = project_workflow_id(request.project_id)
        self.start_calls += 1
        if workflow_id not in self.started:
            self.started[workflow_id] = request
            self.states[workflow_id] = ProjectWorkflowState(
                project_id=request.project_id, status="ingesting"
            )
        return workflow_id, f"{workflow_id}-run"

    def cancel_project(self, workflow_id: str) -> None:
        self.cancelled.append(workflow_id)
        state = self.states.get(workflow_id)
        if state is not None:
            self.states[workflow_id] = state.model_copy(
                update={"status": "cancelled", "cancelled": True}
            )

    def describe_project(self, workflow_id: str) -> ProjectWorkflowState | None:
        return self.states.get(workflow_id)

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
        self.cancelled_workflows.append(workflow_id)
        return workflow_id not in self.missing_workflows


class TemporalWorkflowController:
    """Adapter over the existing Temporal client and parent project workflow."""

    def __init__(
        self,
        target_host: str,
        namespace: str = "default",
        *,
        api_key: str | None = None,
        tls_enabled: bool | None = None,
    ) -> None:
        self._target_host = target_host
        self._namespace = namespace
        self._api_key = api_key
        # Temporal Cloud is always TLS. Defaulting to "TLS whenever an API key
        # is configured" means a deployed environment cannot accidentally
        # connect in plaintext, while a local dev server still works.
        self._tls_enabled = tls_enabled if tls_enabled is not None else api_key is not None

    def _run(self, coroutine: object) -> object:
        import asyncio

        return asyncio.run(coroutine)  # type: ignore[arg-type]

    async def _client(self) -> object:
        from temporalio.client import Client, TLSConfig

        return await Client.connect(
            self._target_host,
            namespace=self._namespace,
            api_key=self._api_key,
            tls=TLSConfig() if self._tls_enabled else False,
        )

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
            await handle.signal(ProjectWorkflow.cancel_project)

        self._run(run())

    def describe_project(self, workflow_id: str) -> ProjectWorkflowState | None:
        from temporalio.service import RPCError

        from packages.workflows.project import ProjectWorkflow

        async def run() -> ProjectWorkflowState | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                state = await handle.query(ProjectWorkflow.project_state)
            except RPCError:
                # Query can fail transiently when no worker is currently polling
                # (e.g. immediately after a worker restart). Return None so the
                # dispatcher skips this command and retries on the next pass.
                return None
            return state if isinstance(state, ProjectWorkflowState) else None

        result = self._run(run())
        return result if isinstance(result, ProjectWorkflowState) else None

    def send_shot_command(
        self, workflow_id: str, command: ShotWorkflowCommand
    ) -> ShotWorkflowCommandResult:
        from packages.workflows.shot import ShotWorkflow

        async def run() -> ShotWorkflowCommandResult:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            await handle.signal(ShotWorkflow.command, command)
            result = await handle.query(ShotWorkflow.command_result, command.command_id)
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
            state = await handle.query(ShotWorkflow.shot_state)
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
            await handle.signal(YouTubePublicationWorkflow.cancel_publication)

        self._run(run())

    def describe_publication(self, workflow_id: str) -> PublicationActivityResult | None:
        from packages.workflows.publication import YouTubePublicationWorkflow

        async def run() -> PublicationActivityResult | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(YouTubePublicationWorkflow.state)
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
        from temporalio.service import RPCError

        from packages.workflows.continuity import ContinuityReferenceWorkflow

        async def run() -> bool:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                await handle.signal(ContinuityReferenceWorkflow.approve, signal)
            except RPCError:
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
            state = await handle.query(ContinuityReferenceWorkflow.status)
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
        return self.describe_shot(workflow_id)

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
            state = await handle.query(FinalEditorialQAWorkflow.final_qa_state)
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
            state = await handle.query(RenderWorkflow.render_state)
            return state if isinstance(state, RenderActivityResult) else None

        result = self._run(run())
        return result if isinstance(result, RenderActivityResult) else None

    def cancel_workflow(self, workflow_id: str) -> bool:
        """Ask the cluster to cancel a dispatched workflow.

        A workflow that no longer exists is reported as ``False`` rather than
        raised: the command it belonged to is finished either way, and the
        dispatcher must still be able to settle the row.
        """
        from temporalio.service import RPCError

        async def run() -> bool:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            try:
                await handle.cancel()
            except RPCError:
                return False
            return True

        return bool(self._run(run()))
