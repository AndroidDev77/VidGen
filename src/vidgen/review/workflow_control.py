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

from vidgen.contracts.publication import PublicationActivityInput, PublicationActivityResult
from vidgen.contracts.shot_workflow import (
    ShotWorkflowCommand,
    ShotWorkflowCommandResult,
    ShotWorkflowProgress,
    ShotWorkflowStatus,
)
from vidgen.contracts.workflow import ProjectWorkflowInput, ProjectWorkflowState

TASK_QUEUE = "vidgen-projects"
#: T25 uploads run on their own queue so a multi-hour video cannot starve the
#: ordinary project activities.
PUBLISHER_TASK_QUEUE = "vidgen-publisher"


def project_workflow_id(project_id: UUID) -> str:
    """Return the stable per-project workflow ID a retried start reuses."""
    return f"vidgen-project-{project_id}"


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
        from temporalio.common import WorkflowIDReusePolicy

        from packages.workflows.project import ProjectWorkflow

        workflow_id = project_workflow_id(request.project_id)

        async def run() -> tuple[str, str]:
            client = await self._client()
            handle = await client.start_workflow(  # type: ignore[attr-defined]
                ProjectWorkflow.run,
                request,
                id=workflow_id,
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
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
        from packages.workflows.project import ProjectWorkflow

        async def run() -> ProjectWorkflowState | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
            state = await handle.query(ProjectWorkflow.project_state)
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

        from packages.workflows.publication import YouTubePublicationWorkflow

        workflow_id = publication_workflow_id(request.publication_run_id)

        async def run() -> tuple[str, str]:
            client = await self._client()
            handle = await client.start_workflow(  # type: ignore[attr-defined]
                YouTubePublicationWorkflow.run,
                request,
                id=workflow_id,
                task_queue=PUBLISHER_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
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
