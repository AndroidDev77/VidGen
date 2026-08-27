"""Owner-scoped T19 continuity-reference control plane.

Routes only persist decisions or queue work; provider calls remain in workers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Response, status
from sqlalchemy import insert, select, update

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
QUEUE_NAMESPACE = UUID("a1165030-d56c-4b6d-b728-4bf49622b8ef")


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
    request: ReferenceMutationRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    if_match: str | None,
    idempotency_key: str | None,
) -> ReferenceMutationResponse:
    owned_project(session, project_id, principal)
    key, expected = _require_mutation(idempotency_key, if_match)
    payload = request.model_dump(mode="json")
    replay = idempotency_for(session, principal).replay(operation, str(project_id), key, payload)
    if replay is not None:
        return ReferenceMutationResponse.model_validate(replay)
    body = ReferenceMutationResponse(
        status="queued",
        resource_id=uuid5(QUEUE_NAMESPACE, f"{project_id}:{operation}:{key}"),
        row_version=expected,
        invalidation=_invalidation(session, project_id),
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
    session.execute(
        update(table)
        .where(table.c.id == reference_set_id)
        .values(
            status=decision,
            approved_by=principal.subject if decision == "approved" else None,
            approved_at=now if decision == "approved" else None,
            updated_at=now,
            row_version=expected + 1,
        )
    )
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
    body = ReferenceMutationResponse(
        status=decision,
        resource_id=reference_set_id,
        row_version=expected + 1,
        invalidation=invalidation,
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
