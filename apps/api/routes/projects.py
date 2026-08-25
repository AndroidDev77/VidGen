from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_blob_store, get_session
from apps.api.schemas.projects import (
    CreateProjectRequest,
    ProjectResponse,
    ProjectStatusResponse,
)
from apps.api.schemas.uploads import InitializeUploadRequest, UploadResponse
from apps.api.settings import APISettings, get_settings
from vidgen.db.models import Project, SourceVideo
from vidgen.db.repositories import ProjectRepository
from vidgen.db.upload_models import UploadSession
from vidgen.storage.blob import FilesystemBlobStore
from vidgen.uploads.service import UploadError, UploadService

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDependency = Annotated[Session, Depends(get_session)]
PrincipalDependency = Annotated[Principal, Depends(get_current_user)]
SettingsDependency = Annotated[APISettings, Depends(get_settings)]
BlobDependency = Annotated[FilesystemBlobStore, Depends(get_blob_store)]


def owned_project(session: Session, project_id: UUID, principal: Principal) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.owner_subject != principal.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    request: CreateProjectRequest,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> Project:
    project = Project(
        name=request.name,
        owner_subject=principal.subject,
        status="awaiting_upload",
        target_duration_seconds=request.target_duration_seconds,
        visual_style=request.visual_style,
        humor_intensity=request.humor_intensity,
        settings={},
    )
    ProjectRepository(session).add(project)
    session.commit()
    return project


@router.get("", response_model=list[ProjectResponse])
def list_projects(session: SessionDependency, principal: PrincipalDependency) -> list[Project]:
    return ProjectRepository(session).list_for_owner(principal.subject)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> Project:
    return owned_project(session, project_id, principal)


@router.get("/{project_id}/status", response_model=ProjectStatusResponse)
def get_project_status(
    project_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> ProjectStatusResponse:
    project = owned_project(session, project_id, principal)
    source = session.scalar(
        select(SourceVideo)
        .where(SourceVideo.project_id == project.id)
        .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
    )
    upload = session.scalar(
        select(UploadSession)
        .where(UploadSession.project_id == project.id)
        .order_by(UploadSession.created_at.desc())
    )
    return ProjectStatusResponse(
        project_id=project.id,
        status=project.status,
        source_video_id=source.id if source else None,
        source_asset_id=source.asset_id if source else None,
        upload_status=upload.status if upload else None,
        error_code=upload.error_code if upload else None,
    )


@router.get("/{project_id}/source-video")
def get_source_video(
    project_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> dict[str, object]:
    project = owned_project(session, project_id, principal)
    source = session.scalar(
        select(SourceVideo)
        .where(SourceVideo.project_id == project.id)
        .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source video not found")
    return {
        "id": source.id,
        "asset_id": source.asset_id,
        "filename": source.filename,
        "duration_seconds": source.duration_seconds,
        "width": source.width,
        "height": source.height,
        "frame_rate": source.frame_rate,
        "probe": source.probe,
    }


@router.post(
    "/{project_id}/uploads", response_model=UploadResponse, status_code=status.HTTP_201_CREATED
)
def initialize_upload(
    project_id: UUID,
    request: InitializeUploadRequest,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
    blob_store: BlobDependency,
) -> UploadSession:
    project = owned_project(session, project_id, principal)
    service = UploadService(
        session,
        blob_store,
        settings.upload_root,
        settings.max_upload_bytes,
        settings.allowed_video_types,
    )
    try:
        return service.initialize(
            project=project,
            owner_subject=principal.subject,
            filename=request.filename,
            media_type=request.media_type,
            expected_size=request.expected_size,
            expected_sha256=request.expected_sha256,
            part_size=request.part_size,
        )
    except UploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code
        ) from error
