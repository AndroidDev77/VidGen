from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_blob_store, get_session, get_workflow_controller
from apps.api.schemas.projects import (
    CreateProjectRequest,
    ProjectBudgetResponse,
    ProjectListItemResponse,
    ProjectResponse,
    ProjectStatusResponse,
    SetProjectBudgetRequest,
)
from apps.api.schemas.uploads import InitializeUploadRequest, UploadResponse
from apps.api.settings import APISettings, get_settings
from services.costs.project_budget import (
    BudgetDeployment,
    BudgetError,
    budget_for,
    create_budget,
    set_caps,
    stored_amount,
    validate_caps,
)
from services.narration.voice_profiles import (
    NarrationDeployment,
    VoiceProfileError,
    current_selection,
    select_profile,
)
from vidgen.contracts.review import ApiErrorField
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.models import Asset, Project, SourceVideo
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.workflow_control import WorkflowController
from vidgen.db.repositories import ProjectRepository
from vidgen.db.upload_models import UploadSession
from vidgen.review.errors import ReviewError, validation_failed
from vidgen.review.projections import project_summary
from vidgen.review.versions import RowVersionService
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.uploads.service import UploadError, UploadService

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDependency = Annotated[Session, Depends(get_session)]
PrincipalDependency = Annotated[Principal, Depends(get_current_user)]
SettingsDependency = Annotated[APISettings, Depends(get_settings)]
BlobDependency = Annotated[BlobStore, Depends(get_blob_store)]


def owned_project(session: Session, project_id: UUID, principal: Principal) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.owner_subject != principal.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def _budget_error(error: BudgetError) -> ReviewError:
    """Render a budget failure as the structured validation error the UI reads.

    The owner gets the field that is wrong, a stable machine code and a sentence
    they can act on - the same shape every other T18 validation failure uses.
    """
    fields = (
        [ApiErrorField(field=error.field, code=error.code, message=error.summary)]
        if error.field
        else []
    )
    return validation_failed(error.summary, fields)


def _project_response(session: Session, project: Project) -> ProjectResponse:
    selected = current_selection(session, project)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        status=project.status,
        target_duration_seconds=project.target_duration_seconds,
        visual_style=project.visual_style,
        humor_intensity=project.humor_intensity,
        created_at=project.created_at,
        updated_at=project.updated_at,
        voice_profile_id=selected.voice_profile_id if selected else None,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    request: CreateProjectRequest,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
) -> ProjectResponse:
    """Create a project with its narration voice and its T23 budget.

    Selecting the voice here rather than repairing it later is the whole point:
    a project that reaches T12 without a resolvable voice profile fails inside a
    paid workflow, and that failure used to require a database fix. The budget
    is the same story one stage earlier - every paid activity reserves against
    ``ProjectBudget``, and a project without that row could not reserve at all.

    The caps are validated before anything is inserted, and the budget is
    written in the same transaction as the project: a project never exists
    without the budget its workflow will reserve against.
    """
    deployment = BudgetDeployment.from_settings(settings)
    try:
        validate_caps(request.budget_warning_cap, request.budget_hard_cap, deployment)
    except BudgetError as error:
        raise _budget_error(error) from error
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
    session.flush()
    try:
        create_budget(
            session,
            project,
            warning_cap=request.budget_warning_cap,
            hard_cap=request.budget_hard_cap,
            deployment=deployment,
        )
    except BudgetError as error:
        session.rollback()
        raise _budget_error(error) from error
    if request.voice_profile_id is not None or request.voice_provider is not None:
        try:
            select_profile(
                session,
                project,
                NarrationDeployment.from_settings(settings),
                voice_profile_id=request.voice_profile_id,
                provider=request.voice_provider,
                provider_voice_id=request.voice_provider_voice_id,
            )
        except VoiceProfileError as error:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code
            ) from error
    session.commit()
    return _project_response(session, project)


@router.get("", response_model=list[ProjectListItemResponse])
def list_projects(
    session: SessionDependency, principal: PrincipalDependency
) -> list[ProjectListItemResponse]:
    versions = RowVersionService(session)
    items: list[ProjectListItemResponse] = []
    for project in ProjectRepository(session).list_for_owner(principal.subject):
        summary = project_summary(session, project, versions)
        items.append(
            ProjectListItemResponse(
                id=project.id,
                name=project.name,
                status=project.status,
                target_duration_seconds=project.target_duration_seconds,
                visual_style=project.visual_style,
                humor_intensity=project.humor_intensity,
                created_at=project.created_at,
                updated_at=project.updated_at,
                current_stage=summary.current_stage.value if summary.current_stage else None,
                progress_percentage=summary.progress_percentage,
                committed_cost_amount=summary.committed_cost_amount,
                hard_cap_amount=summary.hard_cap_amount,
                has_failures=summary.has_failures,
                row_version=summary.row_version,
            )
        )
    session.commit()
    return items


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> ProjectResponse:
    return _project_response(session, owned_project(session, project_id, principal))


def _budget_response(budget: ProjectBudget) -> ProjectBudgetResponse:
    """Render the budget at the scale the column stores.

    A value just written is still in memory at whatever scale it was parsed
    with, while one read back carries the column's six decimal places. Rendering
    both at the stored scale means the same budget reads the same way whether or
    not this request is the one that wrote it.
    """
    return ProjectBudgetResponse(
        project_id=budget.project_id,
        warning_cap=stored_amount(budget.warning_cap),
        hard_cap=stored_amount(budget.hard_cap),
        currency=budget.currency,
        policy_version=budget.policy_version,
        reserved_amount=stored_amount(budget.reserved_amount),
        committed_amount=stored_amount(budget.committed_amount),
        released_amount=stored_amount(budget.released_amount),
        row_version=budget.row_version,
    )


@router.get("/{project_id}/budget", response_model=ProjectBudgetResponse)
def get_budget(
    project_id: UUID, session: SessionDependency, principal: PrincipalDependency
) -> ProjectBudgetResponse:
    project = owned_project(session, project_id, principal)
    budget = budget_for(session, project.id)
    if budget is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="budget not found")
    return _budget_response(budget)


@router.put("/{project_id}/budget", response_model=ProjectBudgetResponse)
def set_budget(
    project_id: UUID,
    request: SetProjectBudgetRequest,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
) -> ProjectBudgetResponse:
    """Fund a project that has no budget yet, or move its caps.

    Without this a project created before budgets were required - or created
    with a zero cap for a fake-provider run - could never start on a paid
    deployment, because the only way to get a budget row would be to recreate
    the project. The ledger's recorded amounts are never rewritten here.
    """
    project = owned_project(session, project_id, principal)
    try:
        budget = set_caps(
            session,
            project,
            warning_cap=request.budget_warning_cap,
            hard_cap=request.budget_hard_cap,
            deployment=BudgetDeployment.from_settings(settings),
        )
    except BudgetError as error:
        session.rollback()
        raise _budget_error(error) from error
    session.commit()
    return _budget_response(budget)


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


ControllerDependency = Annotated[WorkflowController, Depends(get_workflow_controller)]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
    blob_store: BlobDependency,
    controller: ControllerDependency,
) -> Response:
    """Delete a project and all of its assets.

    Any running workflow is cancelled first (best-effort). Blob storage keys are
    cleaned up before the database row is removed. All related DB rows cascade.
    """
    project = owned_project(session, project_id, principal)
    # Cancel any live workflow so the worker stops before we remove its data.
    run = session.scalar(
        select(ProjectWorkflowRun).where(ProjectWorkflowRun.project_id == project.id)
    )
    if run is not None and run.status not in ("completed", "cancelled", "failed"):
        try:
            controller.cancel_workflow(run.workflow_id)
        except Exception:
            pass
    # Delete blobs for all assets owned by this project.
    assets = session.scalars(select(Asset).where(Asset.project_id == project.id)).all()
    for asset in assets:
        try:
            blob_store.delete(asset.storage_key)
        except Exception:
            pass
    session.delete(project)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_SUBTITLE_MEDIA_TYPES = frozenset({"text/plain", "application/x-subrip"})
_SUBTITLE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB; SRT files are typically < 100 KB


@router.post(
    "/{project_id}/subtitle-uploads",
    status_code=status.HTTP_201_CREATED,
)
async def upload_subtitle(
    project_id: UUID,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
    blob_store: BlobDependency,
) -> dict:
    """Store a pre-existing SRT subtitle file as an asset.

    The returned ``asset_id`` can be passed to ``workflow:start`` as
    ``subtitle_asset_ids``, which causes transcript acquisition to prefer
    the uploaded file over provider search and Whisper transcription.
    """
    project = owned_project(session, project_id, principal)
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in _SUBTITLE_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unsupported_media_type",
        )
    content = await request.body()
    if len(content) > _SUBTITLE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="upload_too_large",
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="empty_file",
        )
    filename = request.headers.get("x-filename", "subtitles.srt")
    service = AssetService(session, blob_store)
    stored = service.store(
        content=content,
        kind="subtitle",
        media_type="application/x-subrip",
        project_id=project.id,
        metadata={"original_filename": filename},
    )
    session.commit()
    return {"asset_id": str(stored.id)}
