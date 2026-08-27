"""Server-Sent Events and the polling fallback for project progress.

Owner authorization happens before any bytes are streamed. The stream reads
durable events in short transactions, honours ``Last-Event-ID``, deduplicates by
sequence, emits heartbeat comments while idle, and closes cleanly on disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from apps.api.routes._common import (
    LastEventIdDep,
    PrincipalDep,
    SessionDep,
    SessionFactoryDep,
    events_for,
    owned_project,
)
from apps.api.schemas.events import ProjectEventListResponse
from vidgen.contracts.review import ProjectEventProjection
from vidgen.review.events import (
    ProjectEventService,
    deduplicate,
    format_sse,
    heartbeat_comment,
    parse_last_event_id,
)
from vidgen.review.projections import resolve_project

router = APIRouter(prefix="/projects", tags=["events"])

POLL_INTERVAL_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 15.0


def _read_batch(
    factory: sessionmaker[Session],
    project_id: UUID,
    owner_subject: str,
    position: int | None,
) -> list[ProjectEventProjection]:
    """Read one bounded batch of events in its own short transaction."""
    with factory() as session:
        resolve_project(session, project_id, owner_subject)
        batch = deduplicate(ProjectEventService(session).since(project_id, position))
        session.rollback()
    return batch


@router.get("/{project_id}/events", response_model=None)
async def stream_events(
    project_id: UUID,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    last_event_id_header: LastEventIdDep = None,
    session_factory: SessionFactoryDep = None,  # type: ignore[assignment]
    last_event_id: int | None = Query(default=None, ge=0),
    poll: bool = Query(default=False),
    close_after_events: int | None = Query(default=None, ge=1, le=1000),
) -> StreamingResponse | ProjectEventListResponse:
    """Stream bounded project events, or return them once when ``poll`` is set."""
    owned_project(session, project_id, principal)
    cursor = parse_last_event_id(last_event_id_header)
    if cursor is None:
        cursor = last_event_id
    service = events_for(session)
    if poll:
        events = deduplicate(service.since(project_id, cursor))
        return ProjectEventListResponse(
            items=events,
            last_event_id=events[-1].event_id if events else (cursor or 0),
        )

    factory = session_factory
    owner_subject = principal.subject

    async def publish() -> AsyncIterator[str]:
        position = cursor
        delivered: set[int] = set()
        idle = 0.0
        emitted = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                # A short transaction per poll, run off the event loop: the
                # stream neither holds a transaction open nor blocks other
                # requests with synchronous database I/O.
                batch = await run_in_threadpool(
                    _read_batch, factory, project_id, owner_subject, position
                )
                fresh = [event for event in batch if event.event_id not in delivered]
                if fresh:
                    idle = 0.0
                    for event in fresh:
                        delivered.add(event.event_id)
                        position = event.event_id
                        emitted += 1
                        yield format_sse(event)
                        if close_after_events is not None and emitted >= close_after_events:
                            return
                else:
                    idle += POLL_INTERVAL_SECONDS
                    if idle >= HEARTBEAT_INTERVAL_SECONDS:
                        idle = 0.0
                        yield heartbeat_comment()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            return

    return StreamingResponse(
        publish(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
