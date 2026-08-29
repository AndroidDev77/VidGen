"""Shared dependencies for the T18 owner-scoped control-plane routes.

Route handlers stay thin by composing these: HTTP validation happens in the
schemas, owner authorization here, and everything else in the review services.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Response
from sqlalchemy.orm import Session, sessionmaker

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import (
    get_blob_store,
    get_session,
    get_session_factory,
    get_workflow_controller,
)
from apps.api.settings import APISettings, get_settings
from services.review.mutations import ReviewMutationService
from services.review.shot_identity import configuration_identities
from vidgen.db.models import Project
from vidgen.review.events import ProjectEventService
from vidgen.review.idempotency import IdempotencyService
from vidgen.review.projections import resolve_project
from vidgen.review.versions import RowVersionService
from vidgen.review.workflow_control import WorkflowController
from vidgen.storage.blob import BlobStore

SessionDep = Annotated[Session, Depends(get_session)]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
PrincipalDep = Annotated[Principal, Depends(get_current_user)]
SettingsDep = Annotated[APISettings, Depends(get_settings)]
BlobDep = Annotated[BlobStore, Depends(get_blob_store)]
ControllerDep = Annotated[WorkflowController, Depends(get_workflow_controller)]
IfMatchDep = Annotated[str | None, Header(alias="If-Match")]
IdempotencyKeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]
LastEventIdDep = Annotated[str | None, Header(alias="Last-Event-ID")]


def owned_project(session: Session, project_id: UUID, principal: Principal) -> Project:
    """Return the project, or raise the same 404 used for foreign resources."""
    return resolve_project(session, project_id, principal.subject)


def versions_for(session: Session) -> RowVersionService:
    return RowVersionService(session)


def events_for(session: Session) -> ProjectEventService:
    return ProjectEventService(session)


def idempotency_for(session: Session, principal: Principal) -> IdempotencyService:
    return IdempotencyService(session, principal.subject)


def mutations_for(
    session: Session,
    principal: Principal,
    controller: WorkflowController,
    settings: APISettings | None = None,
) -> ReviewMutationService:
    resolved = settings or get_settings()
    # The same configuration identities the T16 fan-out binds into a child
    # workflow ID, so a T18 command reaches the real child. They are composed in
    # the review service so the route layer never names a provider.
    t14_identity, t15_identity = configuration_identities(
        image_provider_name=resolved.image_provider_name,
        image_model=resolved.image_model,
        video_provider_name=resolved.video_provider_name,
        visual_capability_profile=resolved.visual_capability_profile,
    )
    return ReviewMutationService(
        session,
        principal.subject,
        versions_for(session),
        events_for(session),
        controller,
        t14_configuration_identity=t14_identity,
        t15_capability_profile_identity=t15_identity,
    )


def set_etag(response: Response, row_version: int) -> None:
    """Publish the row version a client must echo back in ``If-Match``."""
    response.headers["ETag"] = f'"{row_version}"'
