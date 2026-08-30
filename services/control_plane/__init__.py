"""The T18b durable control plane.

An asynchronous product command is accepted only when it has been persisted as a
:class:`~vidgen.db.control_command_models.ControlCommandRecord` inside the same
transaction as the request that created it. A dispatcher then claims that row
under a lease, starts or signals the real workflow, and writes the workflow's
actual identity back before the command may report itself running.

The package boundary is deliberate:

* ``commands`` is what a FastAPI route calls; it never touches Temporal.
* ``handlers`` is what the dispatcher calls; it never performs provider work.
* ``dispatcher`` owns claiming, leases, bounded attempts and settling.
* ``lineage`` and ``references`` derive the reproducible identities both halves
  agree on, so a route and a worker can never disagree about what a command is.
"""

from services.control_plane.commands import (
    CommandOutcome,
    ControlPlaneService,
    request_digest,
)
from services.control_plane.dispatcher import (
    ControlCommandDispatcher,
    DispatchReport,
    command_status,
    default_dispatcher_id,
)
from services.control_plane.generation_runs import (
    GenerationRunService,
    generation_input_identity,
)
from services.control_plane.handlers import (
    UNSUPPORTED_REMEDIATION_TARGETS,
    DispatchContext,
    DispatchFailure,
    DispatchOutcome,
    dispatch,
)
from services.control_plane.references import reference_run_id, resolve_reference_inputs
from services.control_plane.revisions import RevisionPlan, plan_revision
from services.control_plane.status import command_projection, permitted_actions

__all__ = [
    "UNSUPPORTED_REMEDIATION_TARGETS",
    "CommandOutcome",
    "ControlCommandDispatcher",
    "ControlPlaneService",
    "DispatchContext",
    "DispatchFailure",
    "DispatchOutcome",
    "DispatchReport",
    "GenerationRunService",
    "RevisionPlan",
    "command_projection",
    "command_status",
    "default_dispatcher_id",
    "dispatch",
    "generation_input_identity",
    "permitted_actions",
    "plan_revision",
    "reference_run_id",
    "request_digest",
    "resolve_reference_inputs",
]
