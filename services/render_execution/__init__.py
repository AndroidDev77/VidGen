"""T17b render execution: the production boundary between a queued render job
and T17's deterministic rendering library.

T17 owns *how* a render is produced. T17b owns *that* a queued render job is
executed exactly once, against the project's authoritative inputs, with durable
checkpoints, a lease no second worker can steal, and outputs that are only
declared complete after verification and persistence both succeed.

Every entry point - the local CLI, the Temporal render activity, the
out-of-band worker and the Azure Container Apps Job - calls
:func:`execute_render_job`. There is deliberately no second implementation.
"""

from services.render_execution.commands import execute_render_job, queue_render_job
from services.render_execution.contracts import (
    RenderExecutionCheckpoint,
    RenderExecutionProgress,
    RenderExecutionRequest,
    RenderExecutionResult,
    RenderExecutionStatus,
    RenderInputSelection,
    RenderWorkerResult,
)
from services.render_execution.executor import RenderExecutionError, RenderExecutor

__all__ = [
    "RenderExecutionCheckpoint",
    "RenderExecutionError",
    "RenderExecutionProgress",
    "RenderExecutionRequest",
    "RenderExecutionResult",
    "RenderExecutionStatus",
    "RenderExecutor",
    "RenderInputSelection",
    "RenderWorkerResult",
    "execute_render_job",
    "queue_render_job",
]
