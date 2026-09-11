"""Recording, and clearing, the failure that stopped a project stage.

``pipeline_failure_events`` is what the dashboard reads to say *why* a project
stopped and which stage a retry should re-enter. A workflow that died answers no
query, so this table is the only witness that survives it - and until now only
two stages ever wrote to it, which left a project that died in narration,
storyboard, rendering or final QA correctly marked failed but with no stage
named and nothing to press.

Every stage activity now goes through here. Two rules keep the table honest:

* A stage that records its own richer failure - the T11 and T12 pipelines name
  the validator code that actually failed - wins. This only fills the gap, so it
  never writes a vaguer second row over a specific one.
* A stage that fails and then succeeds on a Temporal retry clears its own row.
  Without that, a recovered stage would leave the dashboard asking an owner to
  retry a project that is working.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from vidgen.contracts.telemetry import FailureClass
from vidgen.db.cost_models import PipelineFailureEvent
from vidgen.review.workflow_control import project_workflow_id
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.provider import record_pipeline_failure

_LOGGER = logging.getLogger("vidgen.worker.stage_failures")


@contextmanager
def _own_session(engine: Engine) -> Iterator[Session]:
    """A session of its own, because the caller's is usually poisoned.

    This runs on the failure path, where the handler's session may hold a failed
    flush. Recording on it would raise a second, less informative error over the
    first.
    """
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _unresolved(session: Session, project_id: UUID, stage: str) -> PipelineFailureEvent | None:
    return session.scalar(
        select(PipelineFailureEvent).where(
            PipelineFailureEvent.project_id == project_id,
            PipelineFailureEvent.stage == stage,
            PipelineFailureEvent.resolved_at.is_(None),
        )
    )


#: T17b classifies a render failure in its own vocabulary. The failure table
#: speaks the telemetry one, so the two are mapped rather than stringified: a
#: classification the dashboard cannot read is a failure it cannot explain.
_RENDER_FAILURE_CLASSES: dict[str, FailureClass] = {
    "lineage": FailureClass.CONTRACT_VALIDATION,
    "validation": FailureClass.CONTRACT_VALIDATION,
    "transient": FailureClass.TRANSPORT,
    "cancelled": FailureClass.CANCELLED,
    "execution": FailureClass.UNKNOWN,
}


def render_failure_class(classification: str) -> FailureClass:
    """The telemetry failure class for a T17b render classification."""
    return _RENDER_FAILURE_CLASSES.get(classification, FailureClass.UNKNOWN)


def record_stage_failure(
    engine: Engine,
    *,
    project_id: UUID,
    stage: str,
    idempotency_key: str,
    error: BaseException | None = None,
    error_code: str | None = None,
    failure_class: FailureClass | None = None,
    retryable: bool | None = None,
) -> None:
    """Record why this stage stopped, unless the stage already said so itself.

    Never raises: a project that just lost a stage must not also lose the
    exception that explains it to a bookkeeping error. A failure that cannot be
    recorded is logged and the original error propagates untouched.
    """
    classification = classify_failure(error) if error is not None else None
    code = error_code or (classification.error_code if classification else "STAGE_FAILED")
    kind = failure_class or (
        classification.failure_class if classification else FailureClass.UNKNOWN
    )
    # Temporal retries an activity until its policy is exhausted, and the
    # workflow only stops on the last attempt. Reporting the last word on
    # whether this is worth retrying is the point, so a caller's explicit value
    # wins over the classifier's guess.
    retry = (
        retryable if retryable is not None else bool(classification and classification.retryable)
    )
    try:
        with _own_session(engine) as session:
            if _unresolved(session, project_id, stage) is not None:
                # The stage recorded its own failure, with a code that names the
                # actual validator or provider outcome. Leave it alone.
                return
            record_pipeline_failure(
                session,
                project_id=project_id,
                workflow_id=project_workflow_id(project_id),
                stage=stage,
                failure_class=kind,
                error_code=code[:64],
                retryable=retry,
                projected_status=f"{stage}_failed"[:32],
                idempotency_key=idempotency_key[:255],
            )
            session.commit()
    except Exception:  # pragma: no cover - never mask the real failure
        _LOGGER.warning(
            "could not record a pipeline failure for a stage that stopped",
            extra={"projectId": str(project_id), "stage": stage},
        )


def resolve_stage_failures(engine: Engine, *, project_id: UUID, stage: str) -> None:
    """Clear this stage's open failures, because it has now succeeded.

    Temporal retries a failed activity, so a stage that recorded a failure and
    then produced its output on a later attempt is not the reason the project is
    stopped. Leaving the row open would put a retry button on a healthy project.
    """
    try:
        with _own_session(engine) as session:
            rows = session.scalars(
                select(PipelineFailureEvent).where(
                    PipelineFailureEvent.project_id == project_id,
                    PipelineFailureEvent.stage == stage,
                    PipelineFailureEvent.resolved_at.is_(None),
                )
            ).all()
            if not rows:
                return
            now = datetime.now(UTC)
            for row in rows:
                row.resolved_at = now
            session.commit()
    except Exception:  # pragma: no cover - a stale row is not worth failing on
        _LOGGER.warning(
            "could not resolve pipeline failures for a stage that succeeded",
            extra={"projectId": str(project_id), "stage": stage},
        )
