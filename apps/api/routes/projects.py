from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete as sql_delete
from sqlalchemy import exc as sql_exc
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.auth import Principal, get_current_user
from apps.api.dependencies import get_blob_store, get_session, get_workflow_controller
from apps.api.schemas.projects import (
    CreateProjectRequest,
    GenerationEstimateRequest,
    GenerationSettingsResponse,
    ProjectBudgetResponse,
    ProjectListItemResponse,
    ProjectResponse,
    ProjectStatusResponse,
    SetGenerationSettingsRequest,
    SetProjectBudgetRequest,
    StageProgressResponse,
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
from services.generation.estimate import estimate_generation_costs
from services.generation.settings import (
    GenerationSettingsError,
    effective_narration_quality_thresholds,
    effective_scene_detection_threshold,
    effective_script_warn_only_validation_codes,
    effective_storyboard_warn_only_validation_codes,
    effective_visual_qa_warn_only_codes,
    effective_warn_only_validation_codes,
    generation_policy_identity,
    project_generation_settings,
    with_generation_settings,
)
from services.narration.voice_profiles import (
    NarrationDeployment,
    VoiceProfileError,
    current_selection,
    select_profile,
)
from services.progress.engine import StageProgress
from services.progress.loaders import load_stage_progress
from services.storyboard.providers import load_capability_profile
from vidgen.contracts.episode_analysis import WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.generation import GenerationCostEstimate, ProjectGenerationSettings
from vidgen.contracts.narration import (
    NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES,
    NarrationQualityThresholds,
)
from vidgen.contracts.review import ApiErrorField
from vidgen.contracts.script import SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.storyboard import STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
from vidgen.contracts.visual_qa import VISUAL_QA_WARN_ONLY_ELIGIBLE_CODES
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.models import Asset, Project, SourceVideo, asset_dependencies
from vidgen.db.repositories import ProjectRepository
from vidgen.db.upload_models import UploadSession
from vidgen.db.workflow_models import ProjectWorkflowRun
from vidgen.review.errors import ReviewError, validation_failed
from vidgen.review.projections import project_summary
from vidgen.review.versions import RowVersionService
from vidgen.review.workflow_control import WorkflowController
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
    generation = project_generation_settings(project)
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
        generation_quality=generation.generation_quality,
        shot_pacing=generation.shot_pacing,
        premium_fallback_allowed=generation.premium_fallback_allowed,
    )


@router.post("/generation-estimate", response_model=GenerationCostEstimate)
def generation_estimate(request: GenerationEstimateRequest) -> GenerationCostEstimate:
    """Estimate video-generation spend per quality mode before a project exists.

    Pure arithmetic over the verified pricing registry and the pacing preset:
    no provider is called and nothing is persisted, so the setup screen can show
    the economy, balanced and premium ranges as the owner moves the controls.
    """
    return estimate_generation_costs(
        target_duration_seconds=request.target_duration_seconds,
        shot_pacing=request.shot_pacing,
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
    generation = request.generation_settings()
    _resolved_narration_quality(generation, settings)
    project = Project(
        name=request.name,
        owner_subject=principal.subject,
        status="awaiting_upload",
        target_duration_seconds=request.target_duration_seconds,
        visual_style=request.visual_style,
        humor_intensity=request.humor_intensity,
        # Written explicitly so the project never depends on the legacy default.
        settings=with_generation_settings({}, generation),
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
                latest_failure_stage=summary.latest_failure_stage,
                latest_failure_code=summary.latest_failure_code,
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


def _generation_settings_response(
    session: Session, project: Project, settings: APISettings
) -> GenerationSettingsResponse:
    generation = project_generation_settings(project)
    storyboard_profile = None
    settings_block = (
        project.settings.get("storyboard") if isinstance(project.settings, dict) else None
    )
    if isinstance(settings_block, dict) and isinstance(
        settings_block.get("capability_profile_id"), str
    ):
        storyboard_profile = settings_block["capability_profile_id"]
    profile = load_capability_profile(storyboard_profile)
    started = (
        session.scalar(
            select(ProjectWorkflowRun.id).where(ProjectWorkflowRun.project_id == project.id)
        )
        is not None
    )
    narration_quality = _resolved_narration_quality(generation, settings)
    return GenerationSettingsResponse(
        project_id=project.id,
        settings=generation,
        generation_policy_identity=generation_policy_identity(
            generation,
            capability_profile_id=profile.capability_profile_id,
            capability_hash=profile.capability_hash,
        ),
        workflow_started=started,
        estimate=estimate_generation_costs(
            target_duration_seconds=project.target_duration_seconds,
            shot_pacing=generation.shot_pacing,
        ),
        effective_scene_detection_threshold=effective_scene_detection_threshold(
            generation, settings.scene_detection_threshold
        ),
        effective_warn_only_validation_codes=sorted(
            effective_warn_only_validation_codes(generation, settings.warn_only_validation_codes)
        ),
        available_warn_only_validation_codes=list(WARN_ONLY_ELIGIBLE_VALIDATION_CODES),
        effective_script_warn_only_validation_codes=sorted(
            effective_script_warn_only_validation_codes(
                generation, settings.script_warn_only_validation_codes
            )
        ),
        available_script_warn_only_validation_codes=list(
            SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
        ),
        effective_narration_quality_thresholds=narration_quality,
        effective_narration_warn_only_quality_codes=list(narration_quality.warn_only_codes),
        available_narration_warn_only_quality_codes=list(
            NARRATION_WARN_ONLY_ELIGIBLE_QUALITY_CODES
        ),
        effective_storyboard_warn_only_validation_codes=sorted(
            effective_storyboard_warn_only_validation_codes(
                generation, settings.storyboard_warn_only_validation_codes
            )
        ),
        available_storyboard_warn_only_validation_codes=list(
            STORYBOARD_WARN_ONLY_ELIGIBLE_VALIDATION_CODES
        ),
        effective_visual_qa_warn_only_codes=sorted(
            effective_visual_qa_warn_only_codes(generation, settings.visual_qa_warn_only_codes)
        ),
        available_visual_qa_warn_only_codes=list(VISUAL_QA_WARN_ONLY_ELIGIBLE_CODES),
    )


def _resolved_narration_quality(
    generation: ProjectGenerationSettings, settings: APISettings
) -> NarrationQualityThresholds:
    """The gate the narration worker will run under, or a 422 naming why it cannot.

    Each limit is valid on its own but the combination may not be - a project
    speaking-rate floor above the deployment ceiling, say - so the resolution
    is checked at the boundary, before anything is written.
    """
    try:
        return effective_narration_quality_thresholds(
            generation, settings.narration_quality_thresholds()
        )
    except GenerationSettingsError as error:
        raise validation_failed(
            str(error),
            [
                ApiErrorField(
                    field="narration_quality_thresholds",
                    code="UNRESOLVABLE_NARRATION_QUALITY",
                    message=str(error),
                )
            ],
        ) from error


@router.get("/{project_id}/generation-settings", response_model=GenerationSettingsResponse)
def get_generation_settings(
    project_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
) -> GenerationSettingsResponse:
    """The project's resolved quality mode, pacing preset and cost estimate."""
    return _generation_settings_response(
        session, owned_project(session, project_id, principal), settings
    )


@router.put("/{project_id}/generation-settings", response_model=GenerationSettingsResponse)
def set_generation_settings(
    project_id: UUID,
    request: SetGenerationSettingsRequest,
    session: SessionDependency,
    principal: PrincipalDependency,
    settings: SettingsDependency,
) -> GenerationSettingsResponse:
    """Replace the project's generation settings.

    The write is whole and explicit. A workflow that is already running keeps
    the identity it started with; the new settings take effect on the next
    generation run, which mints new shot identities rather than reusing outputs
    planned or routed under the old ones.
    """
    project = owned_project(session, project_id, principal)
    generation = request.generation_settings()
    _resolved_narration_quality(generation, settings)
    project.settings = with_generation_settings(project.settings, generation)
    session.flush()
    session.commit()
    return _generation_settings_response(session, project, settings)


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
    progress = load_stage_progress(session, project)
    return ProjectStatusResponse(
        project_id=project.id,
        status=project.status,
        source_video_id=source.id if source else None,
        source_asset_id=source.asset_id if source else None,
        upload_status=upload.status if upload else None,
        error_code=upload.error_code if upload else None,
        stage_progress=_stage_progress_response(progress) if progress else None,
    )


def _stage_progress_response(progress: StageProgress) -> StageProgressResponse:
    return StageProgressResponse(
        stage=progress.stage,
        label=progress.label,
        state=progress.state,
        phase=progress.phase,
        completed_count=progress.completed_count,
        total_count=progress.total_count,
        unit=progress.unit,
        count_label=progress.count_label,
        percentage=progress.percentage,
        message=progress.message,
        error_code=progress.error_code,
        updated_at=progress.updated_at,
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
    # asset_dependencies.parent_asset_id has RESTRICT — clear dependency rows
    # that point to this project's assets before the cascade hits the assets table.
    asset_ids = [a.id for a in assets]
    if asset_ids:
        session.execute(
            sql_delete(asset_dependencies).where(
                asset_dependencies.c.parent_asset_id.in_(asset_ids)
            )
        )
    try:
        session.delete(project)
        session.commit()
    except sql_exc.IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project_has_references",
        ) from exc
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
) -> dict[str, str]:
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
