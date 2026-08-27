"""Durable, ordered, bounded project events for the review UI.

The database is the source of truth for workflow progress: events are appended
from existing checkpoints and projections, ordered by a per-project sequence,
and read back in short transactions so a long-lived Server-Sent Events stream
never holds one open. Redis may fan out delivery later without becoming the
system of record.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vidgen.contracts.review import PipelineStage, ProjectEventProjection
from vidgen.db.review_models import MAX_EVENT_PAYLOAD_BYTES, ProjectUIEvent

# Keys allowed in an event payload. Transcript text, script text, prompts,
# signed URLs, provider responses and media metadata are excluded by design.
ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "progress_percentage",
        "completed_shot_count",
        "total_shot_count",
        "retryable_failure_count",
        "render_status",
        "cost_summary_version",
        "warning_code",
        "failure_code",
    }
)


class EventPayloadTooLarge(ValueError):
    """Raised when a caller tries to append an unbounded event payload."""


class ProjectEventService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        project_id: UUID,
        *,
        event_type: str,
        status: str,
        stage: PipelineStage | None = None,
        workflow_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ProjectUIEvent:
        """Append one bounded event and return it."""
        bounded = {k: v for k, v in (payload or {}).items() if k in ALLOWED_PAYLOAD_KEYS}
        encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":"), default=str)
        if len(encoded.encode()) > MAX_EVENT_PAYLOAD_BYTES:
            raise EventPayloadTooLarge("project event payload exceeds the configured bound")
        next_sequence = (
            self._session.scalar(
                select(func.coalesce(func.max(ProjectUIEvent.sequence), 0)).where(
                    ProjectUIEvent.project_id == project_id
                )
            )
            or 0
        ) + 1
        event = ProjectUIEvent(
            project_id=project_id,
            sequence=next_sequence,
            event_type=event_type,
            stage=stage.value if stage is not None else None,
            status=status,
            workflow_id=workflow_id,
            payload=bounded,
            created_at=datetime.now(UTC),
        )
        self._session.add(event)
        self._session.flush()
        return event

    def since(
        self, project_id: UUID, last_event_id: int | None, limit: int = 200
    ) -> list[ProjectEventProjection]:
        """Return events after ``last_event_id`` in canonical project order."""
        query = select(ProjectUIEvent).where(ProjectUIEvent.project_id == project_id)
        if last_event_id is not None:
            query = query.where(ProjectUIEvent.sequence > last_event_id)
        rows = self._session.scalars(query.order_by(ProjectUIEvent.sequence).limit(limit)).all()
        return [project_event_projection(row) for row in rows]

    def latest_sequence(self, project_id: UUID) -> int:
        return (
            self._session.scalar(
                select(func.coalesce(func.max(ProjectUIEvent.sequence), 0)).where(
                    ProjectUIEvent.project_id == project_id
                )
            )
            or 0
        )


def project_event_projection(row: ProjectUIEvent) -> ProjectEventProjection:
    payload = row.payload or {}
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ProjectEventProjection(
        event_id=row.sequence,
        project_id=row.project_id,
        workflow_id=row.workflow_id,
        event_type=row.event_type,
        stage=PipelineStage(row.stage) if row.stage else None,
        status=row.status,
        progress_percentage=_number(payload.get("progress_percentage")),
        completed_shot_count=_integer(payload.get("completed_shot_count")),
        total_shot_count=_integer(payload.get("total_shot_count")),
        retryable_failure_count=_integer(payload.get("retryable_failure_count")),
        render_status=_text(payload.get("render_status")),
        cost_summary_version=_integer(payload.get("cost_summary_version")),
        warning_code=_text(payload.get("warning_code")),
        failure_code=_text(payload.get("failure_code")),
        created_at=created_at,
    )


def parse_last_event_id(header: str | None) -> int | None:
    """Return the sequence a reconnecting client already received."""
    if header is None:
        return None
    candidate = header.strip()
    if not candidate.isdigit():
        return None
    return int(candidate)


def format_sse(event: ProjectEventProjection) -> str:
    """Render one event in the ``text/event-stream`` wire format."""
    body = event.model_dump_json()
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {body}\n\n"


def heartbeat_comment() -> str:
    """Return the periodic comment that keeps an idle stream and its proxies alive."""
    return ": heartbeat\n\n"


def deduplicate(events: Sequence[ProjectEventProjection]) -> list[ProjectEventProjection]:
    """Drop repeated event IDs while preserving canonical project ordering."""
    seen: set[int] = set()
    ordered: list[ProjectEventProjection] = []
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        ordered.append(event)
    return ordered


def _integer(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _text(value: object) -> str | None:
    return str(value)[:64] if isinstance(value, str) else None
