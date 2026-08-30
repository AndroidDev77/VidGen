"""Owner-scoped narration voice selection.

Project creation used to store empty settings, which meant a browser-created
project could not reach T12 narration at all without a manual database repair.
These routes are the supported product path: list the voices this deployment can
actually narrate with, select one for a project, and read back what is selected.

No response contains a credential, and no request accepts one.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response

from apps.api.routes._common import (
    PrincipalDep,
    SessionDep,
    SettingsDep,
    owned_project,
    set_etag,
    versions_for,
)
from apps.api.schemas.voice_profiles import (
    SelectVoiceProfileRequest,
    VoiceProfileListResponse,
    VoiceProfileResponse,
)
from services.narration.voice_profiles import (
    NarrationDeployment,
    VoiceProfileError,
    available_profiles,
    current_selection,
    select_profile,
)
from vidgen.contracts.review import ApiErrorCode, PipelineStage
from vidgen.review.errors import conflict, not_found
from vidgen.review.events import ProjectEventService

router = APIRouter(prefix="/projects", tags=["voice-profiles"])

#: A voice selection is project-scoped state, so it shares the project's row
#: version rather than inventing a resource type the schema does not know.
VOICE_RESOURCE = "project"


@router.get("/{project_id}/voice-profiles", response_model=VoiceProfileListResponse)
def list_voice_profiles(
    project_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> VoiceProfileListResponse:
    project = owned_project(session, project_id, principal)
    items = available_profiles(session, project, NarrationDeployment.from_settings(settings))
    selected = current_selection(session, project)
    session.commit()
    return VoiceProfileListResponse(
        project_id=project.id,
        items=items,
        selected_voice_profile_id=selected.voice_profile_id if selected else None,
    )


@router.get("/{project_id}/voice-profile", response_model=VoiceProfileResponse)
def get_voice_profile(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> VoiceProfileResponse:
    project = owned_project(session, project_id, principal)
    selected = current_selection(session, project)
    if selected is None:
        raise not_found("voice profile selection")
    return VoiceProfileResponse(project_id=project.id, profile=selected)


@router.put("/{project_id}/voice-profile", response_model=VoiceProfileResponse)
def set_voice_profile(
    project_id: UUID,
    request: SelectVoiceProfileRequest,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    response: Response,
) -> VoiceProfileResponse:
    """Select this project's narration voice.

    Changing an already-selected voice is a material change: T12's generation
    identity binds the profile version and configuration hash, so the next
    narration run is a different run and the lineage below it is rebuilt rather
    than reused. Nothing above narration is affected.
    """
    project = owned_project(session, project_id, principal)
    previous = current_selection(session, project)
    try:
        selected = select_profile(
            session,
            project,
            NarrationDeployment.from_settings(settings),
            voice_profile_id=request.voice_profile_id,
            provider=request.provider,
            provider_voice_id=request.provider_voice_id,
            model=request.model,
            language=request.language,
        )
    except VoiceProfileError as error:
        if error.code == "voice_profile_not_found":
            raise not_found("voice profile") from error
        raise conflict(ApiErrorCode.VALIDATION_FAILED, error.summary) from error
    versions = versions_for(session)
    row_version = versions.bump(project.id, VOICE_RESOURCE, project.id)
    if previous is None or previous.voice_profile_id != selected.voice_profile_id:
        ProjectEventService(session).append(
            project.id,
            event_type="voice_profile_selected",
            status="selected",
            stage=PipelineStage.NARRATION,
            payload={"provider": selected.provider},
        )
    session.commit()
    set_etag(response, row_version)
    return VoiceProfileResponse(project_id=project.id, profile=selected)
