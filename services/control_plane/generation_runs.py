"""Immutable project generation runs: the unit a project restarts from.

Before T18b a project had exactly one workflow execution and no way back once
it closed. A run makes the project's lineage explicit: each material revision or
continuation opens a new run with its own entry stage and input identity, and
the previous run becomes readable history rather than something to overwrite.

The partial unique index on the table allows one non-terminal run per project,
so two concurrent continuations cannot both claim to be the active lineage - the
loser adopts the winner instead of starting a second workflow execution.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import (
    ProjectGenerationRun,
    ProjectGenerationRunStatus,
)
from vidgen.db.control_command_models import ProjectGenerationRunRecord

_TERMINAL = {
    ProjectGenerationRunStatus.COMPLETED,
    ProjectGenerationRunStatus.FAILED,
    ProjectGenerationRunStatus.CANCELLED,
    ProjectGenerationRunStatus.SUPERSEDED,
}


def generation_input_identity(
    *, project_id: UUID, entry_stage: str, material: dict[str, str]
) -> str:
    """The hash that makes two runs over the same material the same run.

    ``material`` carries only identifiers and hashes - a script version, a
    transcript ID, a storyboard run - never the content behind them.
    """
    payload = {
        "project_id": str(project_id),
        "entry_stage": entry_stage,
        "material": dict(sorted(material.items())),
        "identity_version": "project-generation-run/1",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def projection(record: ProjectGenerationRunRecord) -> ProjectGenerationRun:
    return ProjectGenerationRun(
        generation_run_id=record.id,
        project_id=record.project_id,
        sequence=record.sequence,
        status=ProjectGenerationRunStatus(record.status),
        entry_stage=record.entry_stage,
        input_identity=record.input_identity,
        workflow_id=record.workflow_id,
        run_id=record.run_id,
        origin_command_id=record.origin_command_id,
        parent_generation_run_id=record.parent_generation_run_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class GenerationRunService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def active(self, project_id: UUID) -> ProjectGenerationRunRecord | None:
        return self._session.scalar(
            select(ProjectGenerationRunRecord)
            .where(
                ProjectGenerationRunRecord.project_id == project_id,
                ProjectGenerationRunRecord.active.is_(True),
            )
            .order_by(ProjectGenerationRunRecord.sequence.desc())
        )

    def history(self, project_id: UUID) -> list[ProjectGenerationRunRecord]:
        return list(
            self._session.scalars(
                select(ProjectGenerationRunRecord)
                .where(ProjectGenerationRunRecord.project_id == project_id)
                .order_by(ProjectGenerationRunRecord.sequence)
            )
        )

    def open(
        self,
        *,
        project_id: UUID,
        entry_stage: str,
        input_identity: str,
        origin_command_id: UUID | None = None,
    ) -> tuple[ProjectGenerationRunRecord, bool]:
        """Open a run, or adopt the compatible one already active.

        Returns ``(run, created)``. A continuation whose material identity
        matches the active run is the same work, so it adopts that run rather
        than starting a second execution over the same inputs.
        """
        current = self.active(project_id)
        if current is not None and current.input_identity == input_identity:
            return current, False
        parent_id: UUID | None = None
        if current is not None:
            # A genuinely different lineage supersedes the previous one. The
            # superseded row is kept: it is the audit trail of what this
            # project produced before the revision.
            current.status = ProjectGenerationRunStatus.SUPERSEDED.value
            current.active = False
            parent_id = current.id
            self._session.flush()
        sequence = (
            int(
                self._session.scalar(
                    select(ProjectGenerationRunRecord.sequence)
                    .where(ProjectGenerationRunRecord.project_id == project_id)
                    .order_by(ProjectGenerationRunRecord.sequence.desc())
                    .limit(1)
                )
                or 0
            )
            + 1
        )
        record = ProjectGenerationRunRecord(
            id=uuid4(),
            project_id=project_id,
            sequence=sequence,
            status=ProjectGenerationRunStatus.ACTIVE.value,
            entry_stage=entry_stage,
            input_identity=input_identity,
            origin_command_id=origin_command_id,
            parent_generation_run_id=parent_id,
            active=True,
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def bind_workflow(
        self, record: ProjectGenerationRunRecord, *, workflow_id: str, run_id: str | None
    ) -> None:
        """Record the execution that is actually driving this run."""
        record.workflow_id = workflow_id[:255]
        record.run_id = run_id[:255] if run_id else None
        self._session.flush()

    def settle(
        self, record: ProjectGenerationRunRecord, status: ProjectGenerationRunStatus
    ) -> None:
        record.status = status.value
        record.active = status not in _TERMINAL
        self._session.flush()
