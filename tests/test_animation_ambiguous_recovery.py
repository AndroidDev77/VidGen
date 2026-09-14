"""A stranded T15 submission: how it happens, and how it is released.

Covers the incident these changes were written for. A worker shares one async
Runway client across activities that each run their own ``asyncio.run`` loop, so
a submit eventually fails locally with ``RuntimeError: Event loop is closed``.
The adapter used to report every transport failure as an ambiguous outcome,
which parked the shot in ``provider_outcome_ambiguous`` forever and left its cost
reservation held against the project's budget - over a request Runway never saw.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from services.animation.fake_provider import FakeVideoProvider
from services.animation.pipeline import SUBMISSION_NOT_SENT_CODE, AnimationPipeline
from services.animation.pipeline_errors import (
    AmbiguousVideoSubmission,
    VideoSubmissionNotSent,
)
from services.animation.reconciliation import (
    OperatorAttestation,
    reconcile_ambiguous_submissions,
)
from tests.test_animation_pipeline import prepared
from vidgen.contracts.animation import AmbiguousSubmissionOutcome
from vidgen.db.animation_models import AnimationItem, RunwayTask
from vidgen.db.cost_models import CostReservation, ProjectBudget


class FailingRunway(FakeVideoProvider):
    """A Runway-named fake whose first submissions fail before any task exists."""

    name = "runway"

    def __init__(self, error: BaseException, *, failures: int = 1) -> None:
        super().__init__()
        self._error = error
        self._remaining = failures

    async def submit(self, request, prompt_image):
        if self._remaining:
            self._remaining -= 1
            raise self._error
        return await super().submit(request, prompt_image)


def budgeted(tmp_path: Path):
    fixture, shot = prepared(tmp_path)
    fixture.session.add(
        ProjectBudget(
            project_id=fixture.project.id,
            warning_cap=Decimal("5"),
            hard_cap=Decimal("10"),
            currency="USD",
            policy_version="test",
        )
    )
    fixture.session.commit()
    return fixture, shot


def animate(fixture, provider, shot, key: str):
    return asyncio.run(
        AnimationPipeline(
            fixture.session,
            fixture.blobs,
            provider,
            max_polls=2,
            poll_interval_seconds=0,
        ).process(project_id=fixture.project.id, idempotency_key=key, shot_id=shot.id)
    )


def item_and_task(fixture) -> tuple[AnimationItem, RunwayTask]:
    item = fixture.session.scalar(select(AnimationItem))
    assert item is not None
    task = fixture.session.scalar(
        select(RunwayTask)
        .where(RunwayTask.animation_item_id == item.id)
        .order_by(RunwayTask.created_at.desc())
    )
    assert task is not None
    fixture.session.refresh(item)
    fixture.session.refresh(task)
    return item, task


def reservation_statuses(fixture) -> list[str]:
    return [row.status for row in fixture.session.scalars(select(CostReservation)).all()]


def test_a_submission_that_never_left_the_worker_stays_retryable(tmp_path: Path) -> None:
    """The exact failure that stranded the shots, and what it must do instead.

    The request provably never reached Runway, so no remote task can exist:
    the item is an ordinary retryable failure, its reservation goes back, and
    the next attempt resubmits rather than refusing.
    """
    fixture, shot = budgeted(tmp_path)
    provider = FailingRunway(VideoSubmissionNotSent("event loop is closed under the client"))

    with pytest.raises(VideoSubmissionNotSent):
        animate(fixture, provider, shot, "not-sent")

    item, task = item_and_task(fixture)
    assert item.status == "animation_failed"
    assert task.provider_status == "submission_failed"
    assert task.failure_code == SUBMISSION_NOT_SENT_CODE
    assert task.remote_task_id is None
    assert reservation_statuses(fixture) == ["RELEASED"]

    # The same run resubmits: nothing is waiting on a human.
    result = animate(fixture, provider, shot, "not-sent")
    assert result.completed_count == 1
    assert provider.submissions == 1


def test_an_ambiguous_submission_is_still_parked_and_keeps_its_reservation(
    tmp_path: Path,
) -> None:
    """The protection that must survive the fix.

    Runway may hold a task for an ambiguous submission, so resubmitting can
    create and bill a duplicate. The item stays parked and the reservation
    stays held, because it may be covering real spend.
    """
    fixture, shot = budgeted(tmp_path)
    provider = FailingRunway(AmbiguousVideoSubmission("transport failed after sending"))

    with pytest.raises(AmbiguousVideoSubmission):
        animate(fixture, provider, shot, "ambiguous")

    item, task = item_and_task(fixture)
    assert item.status == "provider_outcome_ambiguous"
    assert task.provider_status == "ambiguous"
    assert reservation_statuses(fixture) == ["RESERVED"]

    with pytest.raises(AmbiguousVideoSubmission, match="manual reconciliation"):
        animate(fixture, provider, shot, "ambiguous")
    assert provider.submissions == 0


def strand(tmp_path: Path):
    fixture, shot = budgeted(tmp_path)
    provider = FailingRunway(AmbiguousVideoSubmission("transport failed after sending"))
    with pytest.raises(AmbiguousVideoSubmission):
        animate(fixture, provider, shot, "ambiguous")
    return fixture, shot, provider


def test_reconciliation_without_an_attestation_changes_nothing(tmp_path: Path) -> None:
    """A dry run: it names what is stranded and what each item still needs."""
    fixture, _shot, _ = strand(tmp_path)

    report = reconcile_ambiguous_submissions(
        fixture.session,
        project_id=fixture.project.id,
        attestation=OperatorAttestation(subject="operator@example.test"),
    )

    assert report.examined_count == 1
    assert report.reconciled_count == 0
    assert report.items[0].outcome == AmbiguousSubmissionOutcome.ATTESTATION_REQUIRED
    item, _ = item_and_task(fixture)
    assert item.status == "provider_outcome_ambiguous"
    assert reservation_statuses(fixture) == ["RESERVED"]


def test_reconciliation_refuses_an_attempt_that_owns_a_remote_task(tmp_path: Path) -> None:
    """Attested or not: a task that exists is polled, never released."""
    fixture, _shot, _ = strand(tmp_path)
    _, task = item_and_task(fixture)
    task.remote_task_id = "runway-task-42"
    fixture.session.commit()

    report = reconcile_ambiguous_submissions(
        fixture.session,
        project_id=fixture.project.id,
        attestation=OperatorAttestation(
            subject="operator@example.test", verified_no_remote_task=True
        ),
    )

    assert report.reconciled_count == 0
    assert report.items[0].outcome == AmbiguousSubmissionOutcome.REMOTE_TASK_EXISTS
    assert report.items[0].remote_task_id == "runway-task-42"
    item, _ = item_and_task(fixture)
    assert item.status == "provider_outcome_ambiguous"
    assert reservation_statuses(fixture) == ["RESERVED"]


def test_an_attested_reconciliation_releases_the_shot_and_its_reservation(
    tmp_path: Path,
) -> None:
    """The way out of the stranded state, end to end."""
    fixture, shot, provider = strand(tmp_path)

    report = reconcile_ambiguous_submissions(
        fixture.session,
        project_id=fixture.project.id,
        attestation=OperatorAttestation(
            subject="operator@example.test",
            verified_no_remote_task=True,
            note="no task for this window on the Runway dashboard",
        ),
    )
    fixture.session.commit()

    assert report.reconciled_count == 1
    outcome = report.items[0]
    assert outcome.outcome == AmbiguousSubmissionOutcome.RECONCILED
    assert outcome.released_reservation_id is not None
    assert report.remaining_count == 0
    assert reservation_statuses(fixture) == ["RELEASED"]

    item, task = item_and_task(fixture)
    assert item.status == "animation_failed"
    assert task.provider_status == "submission_failed"
    # The release is attributable: who said so, on what evidence.
    assert task.response_metadata["reconciled_by"] == "operator@example.test"
    assert "dashboard" in task.response_metadata["reconciliation_note"]

    # And the shot's next attempt resubmits instead of refusing.
    result = animate(fixture, provider, shot, "ambiguous")
    assert result.completed_count == 1


def test_reconciliation_can_be_scoped_to_one_shot(tmp_path: Path) -> None:
    fixture, _, _ = strand(tmp_path)

    report = reconcile_ambiguous_submissions(
        fixture.session,
        project_id=fixture.project.id,
        attestation=OperatorAttestation(
            subject="operator@example.test", verified_no_remote_task=True
        ),
        shot_ids=(fixture.project.id,),
    )

    assert report.examined_count == 0
    assert reservation_statuses(fixture) == ["RESERVED"]


class RefusedByRunway(RuntimeError):
    """A provider refusal: Runway answered, so it created nothing."""

    status_code = 400


def test_a_refusal_the_provider_answered_releases_its_reservation(tmp_path: Path) -> None:
    """The provider received the request and made no task, so the money goes back."""
    fixture, shot = budgeted(tmp_path)
    provider = FailingRunway(RefusedByRunway("unsupported ratio"))

    with pytest.raises(RefusedByRunway):
        animate(fixture, provider, shot, "refused")

    item, task = item_and_task(fixture)
    assert item.status == "animation_failed"
    assert task.provider_status == "submission_failed"
    assert reservation_statuses(fixture) == ["RELEASED"]


def test_every_resubmittable_submission_failure_releases_its_reservation(
    tmp_path: Path,
) -> None:
    """The release follows the resubmission decision, so the two never disagree.

    Marking the task ``submission_failed`` is what lets the next attempt submit
    again. Holding the reservation for a submit that will be redone from scratch
    would leak one per attempt, with no way back to it.
    """
    fixture, shot = budgeted(tmp_path)
    provider = FailingRunway(RuntimeError("provider client misbehaved"), failures=2)

    for key in ("leaky", "leaky"):
        with pytest.raises(RuntimeError):
            animate(fixture, provider, shot, key)

    _, task = item_and_task(fixture)
    assert task.provider_status == "submission_failed"
    # Two attempts, two reservations, and neither is still held.
    assert reservation_statuses(fixture) == ["RELEASED", "RELEASED"]
