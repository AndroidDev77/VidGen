from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_blob_store, get_session
from apps.api.schemas.uploads import DownloadURLResponse
from vidgen.db.models import Asset, Project
from vidgen.storage.blob import BlobStore

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/{asset_id}/download-url", response_model=DownloadURLResponse)
def get_download_url(
    asset_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(get_current_user)],
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    expires_in_seconds: int = Query(default=900, ge=60, le=3600),
) -> DownloadURLResponse:
    asset = session.get(Asset, asset_id)
    project = session.get(Project, asset.project_id) if asset and asset.project_id else None
    if asset is None or project is None or project.owner_subject != principal.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    return DownloadURLResponse(
        asset_id=asset.id,
        url=blob_store.signed_read_url(asset.storage_key, expires_in_seconds),
        expires_in_seconds=expires_in_seconds,
    )
