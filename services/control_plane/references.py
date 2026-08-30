"""Resolving the T19 reference run for a project.

The reference run is derived, never allocated: it is a UUID5 of the project's
authoritative episode analysis and storyboard. Two consequences make the whole
T19 integration restartable:

* the project workflow, the reference build command and the approval command all
  compute the *same* reference run for the same inputs, so they all address the
  same Temporal workflow;
* a storyboard change produces a different reference run, so a revised project
  drafts new references instead of resuming a pause that belonged to the cut it
  replaced.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from vidgen.contracts.continuity_workflow import ReferenceWorkflowInput
from vidgen.db.continuity_repository import ContinuityRepository, LineageFailure

#: Fixed namespace so a reference run keeps one ID across restarts and across a
#: rebuilt database.
REFERENCE_RUN_NAMESPACE = UUID("0d1f5a9c-3f2a-5c48-9b6e-2d7c4a8e1b03")


class ReferenceInputsUnavailable(RuntimeError):
    """The project has no authoritative inputs for a reference run yet."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def reference_run_id(*, episode_analysis_id: UUID, storyboard_run_id: UUID) -> UUID:
    return uuid5(REFERENCE_RUN_NAMESPACE, f"{episode_analysis_id}:{storyboard_run_id}")


def resolve_reference_inputs(
    session: Session, *, project_id: UUID, idempotency_key: str
) -> ReferenceWorkflowInput:
    """Build the compact, ID-only message the T19 workflow is started with."""
    try:
        analysis, storyboard = ContinuityRepository(session).authoritative_inputs(project_id)
    except LineageFailure as error:
        raise ReferenceInputsUnavailable(
            error.code,
            "This project has no authoritative episode analysis and storyboard yet.",
        ) from error
    return ReferenceWorkflowInput(
        project_id=project_id,
        episode_analysis_id=analysis.id,
        storyboard_run_id=storyboard.id,
        reference_run_id=reference_run_id(
            episode_analysis_id=analysis.id, storyboard_run_id=storyboard.id
        ),
        idempotency_key=idempotency_key[:255],
    )
