"""Owner-scoped storyboard retrieval.

The T13 timing manifest remains the timing authority: T18 presents it read-only
and never re-times or reorders shots.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response

from apps.api.routes._common import (
    PrincipalDep,
    SessionDep,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.storyboards import StoryboardResponse
from vidgen.review.projections import selected_storyboard, storyboard_projection

router = APIRouter(prefix="/projects", tags=["storyboards"])


@router.get("/{project_id}/storyboard", response_model=StoryboardResponse)
def get_storyboard(
    project_id: UUID, session: SessionDep, principal: PrincipalDep, response: Response
) -> StoryboardResponse:
    project = owned_project(session, project_id, principal)
    run = selected_storyboard(session, project.id)
    body = storyboard_projection(session, project.id, run, versions_for(session))
    session.commit()
    set_etag(response, body.row_version)
    return body
