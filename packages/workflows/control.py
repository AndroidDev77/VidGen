"""Small, replay-safe workflows that give a T18b control command a real owner.

T22 and T17b already have canonical activities: the project workflow calls them
in sequence. A *manual* final-QA run or a *rerender* requested from the review
UI needs the same execution, driven by something durable that outlives the HTTP
request. These workflows are that owner - nothing more.

They deliberately add no logic. Each starts exactly one existing activity, on
exactly the queue that activity belongs on, and exposes its bounded result as a
query so the control command can report truthful progress without polling the
database for it. Every message is IDs only.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from packages.workflows.retry_policies import (
        default_activity_retry_policy,
        provider_activity_retry_policy,
    )
    from vidgen.contracts.workflow import (
        FinalQAActivityInput,
        FinalQAActivityResult,
        RenderActivityInput,
        RenderActivityResult,
    )

#: The dedicated render queue, mirrored from the project workflow so a manual
#: rerender competes for the same bounded CPU budget as an automatic one.
RENDER_TASK_QUEUE = "render"


def final_qa_workflow_id(project_id: object, idempotency_key: str) -> str:
    """The stable ID of a manual T22 run. A replayed command adopts it."""
    return f"vidgen-final-qa-{project_id}-{idempotency_key}"[:255]


def render_workflow_id(project_id: object, idempotency_key: str) -> str:
    """The stable ID of a requested rerender. A replayed command adopts it."""
    return f"vidgen-render-{project_id}-{idempotency_key}"[:255]


@workflow.defn
class FinalEditorialQAWorkflow:
    """Run T22 once against the project's current selected render."""

    def __init__(self) -> None:
        self._result: FinalQAActivityResult | None = None

    @workflow.run
    async def run(self, request: FinalQAActivityInput) -> FinalQAActivityResult:
        self._result = await workflow.execute_activity(
            "run_final_editorial_qa_activity",
            request,
            result_type=FinalQAActivityResult,
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=provider_activity_retry_policy(),
        )
        return self._result

    @workflow.query
    def final_qa_state(self) -> FinalQAActivityResult | None:
        """IDs, counts and the current decision. Never a report or a finding."""
        return self._result


@workflow.defn
class RenderWorkflow:
    """Drive one T17b render job through the canonical executor.

    A retry of the activity resumes the same render job from its durable
    checkpoint, so restarting this workflow never produces a second render.
    """

    def __init__(self) -> None:
        self._result: RenderActivityResult | None = None

    @workflow.run
    async def run(self, request: RenderActivityInput) -> RenderActivityResult:
        self._result = await workflow.execute_activity(
            "run_render_activity",
            request,
            result_type=RenderActivityResult,
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=default_activity_retry_policy(),
            task_queue=RENDER_TASK_QUEUE,
        )
        return self._result

    @workflow.query
    def render_state(self) -> RenderActivityResult | None:
        """Query-visible T17b progress: IDs, status and percentage only."""
        return self._result
