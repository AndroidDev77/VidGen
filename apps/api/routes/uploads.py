from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_blob_store, get_session
from apps.api.schemas.uploads import (
    CompleteUploadResponse,
    UploadPartResponse,
    UploadResponse,
)
from apps.api.settings import APISettings, get_settings
from vidgen.db.models import Asset
from vidgen.db.repositories import UploadRepository
from vidgen.db.upload_models import UploadSession
from vidgen.storage.blob import FilesystemBlobStore
from vidgen.uploads.service import UploadError, UploadService

router = APIRouter(prefix="/uploads", tags=["uploads"])

SessionDependency = Annotated[Session, Depends(get_session)]
PrincipalDependency = Annotated[Principal, Depends(get_current_user)]
SettingsDependency = Annotated[APISettings, Depends(get_settings)]
BlobDependency = Annotated[FilesystemBlobStore, Depends(get_blob_store)]


def owned_upload(session: Session, upload_id: UUID, principal: Principal) -> UploadSession:
    upload = UploadRepository(session).get(upload_id)
    if upload is None or upload.owner_subject != principal.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="upload not found")
    return upload


def service_for(
    session: Session, settings: APISettings, blob_store: FilesystemBlobStore
) -> UploadService:
    return UploadService(
        session,
        blob_store,
        settings.upload_root,
        settings.max_upload_bytes,
        settings.allowed_video_types,
    )


@router.get("/{upload_id}", response_model=UploadResponse)
def get_upload(
    upload_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> UploadSession:
    return owned_upload(session, upload_id, principal)


@router.put("/{upload_id}/parts/{part_number}", response_model=UploadPartResponse)
async def upload_part(
    upload_id: UUID,
    part_number: int,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
    blob_store: BlobDependency,
) -> UploadPartResponse:
    upload = owned_upload(session, upload_id, principal)
    try:
        result = await service_for(session, settings, blob_store).write_part(
            upload, part_number, request.stream()
        )
    except UploadError as error:
        code = (
            status.HTTP_409_CONFLICT
            if error.code == "conflicting_part"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=code, detail=error.code) from error
    return UploadPartResponse(
        upload_id=upload.id,
        part_number=result.part.part_number,
        byte_size=result.part.byte_size,
        sha256=result.part.sha256,
        duplicate=result.duplicate,
    )


@router.post("/{upload_id}/complete", response_model=CompleteUploadResponse)
def complete_upload(
    upload_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
    blob_store: BlobDependency,
) -> CompleteUploadResponse:
    upload = owned_upload(session, upload_id, principal)
    try:
        result = service_for(session, settings, blob_store).finalize(upload)
    except UploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code
        ) from error
    asset = result.source_video.asset_id
    stored_asset = session.get(Asset, asset)
    if stored_asset is None:
        raise HTTPException(status_code=500, detail="source asset missing")
    return CompleteUploadResponse(
        upload_id=result.upload.id,
        source_video_id=result.source_video.id,
        asset_id=stored_asset.id,
        sha256=stored_asset.sha256,
        byte_size=stored_asset.byte_size,
        status=result.upload.status,
    )
