"""Owner-scoped T19 continuity-reference control plane.

Routes persist decisions and create durable control commands; provider calls
stay in workers. Nothing here answers ``202 Accepted`` without first writing a
command row a dispatcher can claim, so a queued reference build is executable
work rather than a calculated identifier.

An approval does two things in one transaction: it records the owner's decision
on the reference set, and it creates the command that delivers that decision to
the T19 workflow which is durably waiting for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Response, status
from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult

from apps.api.routes._common import (
    IdempotencyKeyDep,
    IfMatchDep,
    PrincipalDep,
    SessionDep,
    idempotency_for,
    owned_project,
    set_etag,
)
from apps.api.schemas.references import (
    ReferenceCollectionResponse,
    ReferenceDecisionRequest,
    ReferenceInvalidationProjection,
    ReferenceMutationRequest,
    ReferenceMutationResponse,
)
from services.control_plane.commands import ControlPlaneService
from vidgen.contracts.control_commands import (
    ControlCommandTargetType,
    ControlCommandType,
)
from vidgen.contracts.review import ApiErrorCode
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_candidates,
    character_reference_sets,
    location_identity_versions,
    location_reference_candidates,
    location_reference_sets,
    reference_approvals,
    shot_reference_bindings,
)
from vidgen.db.models import Character, Location
from vidgen.review.errors import conflict, not_found

router = APIRouter(prefix="/projects", tags=["references"])


def _rows(session: SessionDep, table: Any, project_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in session.execute(select(table).where(table.c.project_id == project_id)).mappings()
    ]


def _invalidation(
    session: SessionDep, project_id: UUID, entity_id: UUID | None = None
) -> ReferenceInvalidationProjection:
    bindings = _rows(session, shot_reference_bindings, project_id)
    affected: list[UUID] = []
    preserved: list[UUID] = []
    for binding in bindings:
        bundle = binding["bundle"]
        dependencies = {
            UUID(value["entity_id"])
            for value in bundle.get("references", [])
            if value.get("entity_id")
        }
        (affected if entity_id in dependencies else preserved).append(binding["storyboard_shot_id"])
    return ReferenceInvalidationProjection(
        affected_shot_ids=sorted(affected, key=str), preserved_shot_ids=sorted(preserved, key=str)
    )


@router.get("/{project_id}/references", response_model=ReferenceCollectionResponse)
def references(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ReferenceCollectionResponse:
    owned_project(session, project_id, principal)
    return ReferenceCollectionResponse(
        project_id=project_id,
        characters=_rows(session, character_identity_versions, project_id),
        locations=_rows(session, location_identity_versions, project_id),
        bindings=_rows(session, shot_reference_bindings, project_id),
    )


def _queue(
    *,
    project_id: UUID,
    operation: str,
    command_type: ControlCommandType,
    target_type: ControlCommandTargetType,
    target_id: UUID | None,
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> ReferenceMutationResponse:
    """Record one durable T19 command and return its real identity.

    The command is created inside this request's transaction: by the time the
    response is written, the work is claimable, queryable by command ID, and
    guaranteed to reach a terminal state on its own.
    """
    project = owned_project(session, project_id, principal)
    key, expected = _require_mutation(idempotency_key, if_match)
    payload = request.model_dump(mode="json")
    replay = idempotency_for(session, principal).replay(operation, str(project_id), key, payload)
    if replay is not None:
        return ReferenceMutationResponse.model_validate(replay)
    outcome = ControlPlaneService(session, principal.subject).submit(
        project,
        command_type=command_type,
        target_type=target_type,
        target_id=target_id or project.id,
        idempotency_key=f"{operation}:{key}"[:255],
        payload={"operation": operation, **payload},
        expected_row_version=expected,
        metadata={"operation": operation, "provider": request.provider},
    )
    body = ReferenceMutationResponse(
        status="queued",
        resource_id=outcome.command.command_id,
        row_version=expected,
        invalidation=_invalidation(session, project_id),
        command_id=outcome.command.command_id,
        command_status=outcome.command.status.value,
        workflow_id=outcome.command.workflow_id,
    )
    idempotency_for(session, principal).record(
        operation,
        str(project_id),
        key,
        payload,
        status.HTTP_202_ACCEPTED,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected)
    return body


@router.post(
    "/{project_id}/references:build",
    response_model=ReferenceMutationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def build_references(
    project_id: UUID,
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _queue(
        project_id=project_id,
        operation="references:build",
        command_type=ControlCommandType.REFERENCE_BUILD,
        target_type=ControlCommandTargetType.PROJECT,
        target_id=None,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.get("/{project_id}/characters")
def characters(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    return [
        {"id": item.id, "canonical_name": item.canonical_name}
        for item in session.scalars(
            select(Character)
            .where(Character.project_id == project_id)
            .order_by(Character.canonical_name)
        )
    ]


@router.get("/{project_id}/locations")
def locations(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    return [
        {"id": item.id, "canonical_name": item.canonical_name}
        for item in session.scalars(
            select(Location)
            .where(Location.project_id == project_id)
            .order_by(Location.canonical_name)
        )
    ]


def _entity(
    session: SessionDep, project_id: UUID, kind: Literal["character", "location"], entity_id: UUID
) -> Any:
    model = Character if kind == "character" else Location
    value = session.scalar(
        select(model).where(model.id == entity_id, model.project_id == project_id)
    )
    if value is None:
        raise not_found(kind)
    return value


@router.get("/{project_id}/characters/{entity_id}")
def character(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    owned_project(session, project_id, principal)
    value = _entity(session, project_id, "character", entity_id)
    return {"id": value.id, "canonical_name": value.canonical_name, "definition": value.definition}


@router.get("/{project_id}/locations/{entity_id}")
def location(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    owned_project(session, project_id, principal)
    value = _entity(session, project_id, "location", entity_id)
    return {"id": value.id, "canonical_name": value.canonical_name, "definition": value.definition}


def _related(
    session: SessionDep,
    project_id: UUID,
    entity_id: UUID,
    kind: Literal["character", "location"],
    candidates: bool,
) -> list[dict[str, Any]]:
    versions = character_identity_versions if kind == "character" else location_identity_versions
    candidate_table = (
        character_reference_candidates if kind == "character" else location_reference_candidates
    )
    entity_column = versions.c.character_id if kind == "character" else versions.c.location_id
    if not candidates:
        return [
            dict(row)
            for row in session.execute(
                select(versions).where(
                    versions.c.project_id == project_id, entity_column == entity_id
                )
            ).mappings()
        ]
    statement = (
        select(candidate_table)
        .join(versions, candidate_table.c.identity_version_id == versions.c.id)
        .where(versions.c.project_id == project_id, entity_column == entity_id)
    )
    return [dict(row) for row in session.execute(statement).mappings()]


@router.get("/{project_id}/characters/{entity_id}/reference-candidates")
def character_candidates(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "character", entity_id)
    return _related(session, project_id, entity_id, "character", True)


@router.get("/{project_id}/characters/{entity_id}/reference-versions")
def character_versions(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "character", entity_id)
    return _related(session, project_id, entity_id, "character", False)


@router.post(
    "/{project_id}/characters/{entity_id}/references:generate",
    response_model=ReferenceMutationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_character_reference(
    project_id: UUID,
    entity_id: UUID,
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "character", entity_id)
    return _queue(
        project_id=project_id,
        operation=f"character:{entity_id}:generate",
        command_type=ControlCommandType.REFERENCE_GENERATE,
        target_type=ControlCommandTargetType.CHARACTER,
        target_id=entity_id,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.get("/{project_id}/locations/{entity_id}/reference-candidates")
def location_candidates(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "location", entity_id)
    return _related(session, project_id, entity_id, "location", True)


@router.get("/{project_id}/locations/{entity_id}/reference-versions")
def location_versions(
    project_id: UUID, entity_id: UUID, session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "location", entity_id)
    return _related(session, project_id, entity_id, "location", False)


@router.post(
    "/{project_id}/locations/{entity_id}/references:generate",
    response_model=ReferenceMutationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_location_reference(
    project_id: UUID,
    entity_id: UUID,
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    owned_project(session, project_id, principal)
    _entity(session, project_id, "location", entity_id)
    return _queue(
        project_id=project_id,
        operation=f"location:{entity_id}:generate",
        command_type=ControlCommandType.REFERENCE_GENERATE,
        target_type=ControlCommandTargetType.LOCATION,
        target_id=entity_id,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


def _require_mutation(idempotency_key: str | None, if_match: str | None) -> tuple[str, int]:
    if not idempotency_key:
        raise conflict(ApiErrorCode.IDEMPOTENCY_KEY_REQUIRED, "Idempotency-Key is required")
    if not if_match:
        raise conflict(ApiErrorCode.PRECONDITION_REQUIRED, "If-Match is required")
    try:
        return idempotency_key, int(if_match.strip('W/"'))
    except ValueError as exc:
        raise conflict(
            ApiErrorCode.VERSION_CONFLICT, "If-Match must contain an integer version"
        ) from exc


def _decide(
    *,
    project_id: UUID,
    entity_id: UUID,
    reference_set_id: UUID,
    kind: Literal["character", "location"],
    decision: Literal["approved", "rejected"],
    request: ReferenceDecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> ReferenceMutationResponse:
    owned_project(session, project_id, principal)
    _entity(session, project_id, kind, entity_id)
    key, expected = _require_mutation(idempotency_key, if_match)
    table = character_reference_sets if kind == "character" else location_reference_sets
    row = (
        session.execute(
            select(table).where(table.c.id == reference_set_id, table.c.project_id == project_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise not_found("reference version")
    identity = character_identity_versions if kind == "character" else location_identity_versions
    identity_row = (
        session.execute(select(identity).where(identity.c.id == row["identity_version_id"]))
        .mappings()
        .one()
    )
    entity_column = identity.c.character_id if kind == "character" else identity.c.location_id
    if identity_row[entity_column.name] != entity_id:
        raise not_found("reference version")
    replay = idempotency_for(session, principal).replay(
        f"reference:{decision}", str(reference_set_id), key, request.model_dump(mode="json")
    )
    if replay is not None:
        return ReferenceMutationResponse.model_validate(replay)
    if row["row_version"] != expected:
        raise conflict(
            ApiErrorCode.VERSION_CONFLICT,
            "Reference version changed",
            current_version=row["row_version"],
        )
    invalidation = _invalidation(session, project_id, entity_id)
    if (
        decision == "approved"
        and invalidation.affected_shot_ids
        and not request.confirm_invalidation
    ):
        raise conflict(
            ApiErrorCode.VALIDATION_FAILED,
            "Confirm the exact downstream invalidation set",
        )
    now = datetime.now(UTC)
    update_result = cast(
        CursorResult[Any],
        session.execute(
            update(table)
            .where(table.c.id == reference_set_id, table.c.row_version == expected)
            .values(
                status=decision,
                approved_by=principal.subject if decision == "approved" else None,
                approved_at=now if decision == "approved" else None,
                updated_at=now,
                row_version=expected + 1,
            )
        ),
    )
    # The read above is only used to construct the response and invalidation preview.
    # Concurrency is enforced by the write itself so two workers cannot both approve
    # the same version after observing the same ETag.
    if update_result.rowcount != 1:
        session.rollback()
        current_version = session.scalar(
            select(table.c.row_version).where(
                table.c.id == reference_set_id, table.c.project_id == project_id
            )
        )
        raise conflict(
            ApiErrorCode.VERSION_CONFLICT,
            "Reference version changed",
            current_version=current_version,
        )
    command_id: UUID | None = None
    command_status: str | None = None
    workflow_id: str | None = None
    if decision == "approved":
        session.execute(
            insert(reference_approvals).values(
                id=uuid4(),
                project_id=project_id,
                reference_set_id=reference_set_id,
                reference_kind=kind,
                identity_version_id=row["identity_version_id"],
                approved_by=principal.subject,
                upstream_lineage_hash=request.upstream_lineage_hash,
                approved_at=now,
                idempotency_key=key,
                created_at=now,
            )
        )
        # The decision is only half the work. Without this command the approval
        # would be a row update with nothing waiting on it - which is exactly
        # the gap T18b closes: the command delivers the approval to the T19
        # workflow that is durably paused, and resumes binding.
        project = owned_project(session, project_id, principal)
        outcome = ControlPlaneService(session, principal.subject).submit(
            project,
            command_type=ControlCommandType.REFERENCE_APPLY,
            target_type=ControlCommandTargetType.REFERENCE_SET,
            target_id=reference_set_id,
            idempotency_key=f"reference:approve:{reference_set_id}:{key}"[:255],
            payload={
                "reference_set_id": str(reference_set_id),
                "upstream_lineage_hash": request.upstream_lineage_hash,
            },
            expected_row_version=expected + 1,
            metadata={"reference_kind": kind, "decision": decision},
        )
        command_id = outcome.command.command_id
        command_status = outcome.command.status.value
        workflow_id = outcome.command.workflow_id
    body = ReferenceMutationResponse(
        status=decision,
        resource_id=reference_set_id,
        row_version=expected + 1,
        invalidation=invalidation,
        command_id=command_id,
        command_status=command_status,
        workflow_id=workflow_id,
    )
    idempotency_for(session, principal).record(
        f"reference:{decision}",
        str(reference_set_id),
        key,
        request.model_dump(mode="json"),
        status.HTTP_200_OK,
        body.model_dump(mode="json"),
    )
    session.commit()
    set_etag(response, expected + 1)
    return body


@router.post(
    "/{project_id}/characters/{entity_id}/references/{reference_set_id}:approve",
    response_model=ReferenceMutationResponse,
)
def approve_character(
    project_id: UUID,
    entity_id: UUID,
    reference_set_id: UUID,
    request: ReferenceDecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _decide(
        project_id=project_id,
        entity_id=entity_id,
        reference_set_id=reference_set_id,
        kind="character",
        decision="approved",
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/locations/{entity_id}/references/{reference_set_id}:approve",
    response_model=ReferenceMutationResponse,
)
def approve_location(
    project_id: UUID,
    entity_id: UUID,
    reference_set_id: UUID,
    request: ReferenceDecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _decide(
        project_id=project_id,
        entity_id=entity_id,
        reference_set_id=reference_set_id,
        kind="location",
        decision="approved",
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/characters/{entity_id}/references/{reference_set_id}:reject",
    response_model=ReferenceMutationResponse,
)
def reject_character(
    project_id: UUID,
    entity_id: UUID,
    reference_set_id: UUID,
    request: ReferenceDecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _decide(
        project_id=project_id,
        entity_id=entity_id,
        reference_set_id=reference_set_id,
        kind="character",
        decision="rejected",
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/locations/{entity_id}/references/{reference_set_id}:reject",
    response_model=ReferenceMutationResponse,
)
def reject_location(
    project_id: UUID,
    entity_id: UUID,
    reference_set_id: UUID,
    request: ReferenceDecisionRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _decide(
        project_id=project_id,
        entity_id=entity_id,
        reference_set_id=reference_set_id,
        kind="location",
        decision="rejected",
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/references:apply",
    response_model=ReferenceMutationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def apply_references(
    project_id: UUID,
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: IfMatchDep = None,
    idempotency_key: IdempotencyKeyDep = None,
) -> ReferenceMutationResponse:
    return _queue(
        project_id=project_id,
        operation="references:apply",
        command_type=ControlCommandType.REFERENCE_APPLY,
        target_type=ControlCommandTargetType.PROJECT,
        target_id=None,
        request=request,
        session=session,
        principal=principal,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )


@router.get("/{project_id}/shots/{shot_id}/continuity")
def shot_continuity(
    project_id: UUID, shot_id: UUID, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    owned_project(session, project_id, principal)
    row = (
        session.execute(
            select(shot_reference_bindings).where(
                shot_reference_bindings.c.project_id == project_id,
                shot_reference_bindings.c.storyboard_shot_id == shot_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise not_found("shot continuity bundle")
    return dict(row)


@router.get("/{project_id}/references/invalidation", response_model=ReferenceInvalidationProjection)
def invalidation(
    project_id: UUID, session: SessionDep, principal: PrincipalDep
) -> ReferenceInvalidationProjection:
    owned_project(session, project_id, principal)
    return _invalidation(session, project_id)
