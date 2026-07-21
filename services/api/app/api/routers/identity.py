"""Authenticated ``/v1`` identity endpoints.

These endpoints expose the current authenticated user and the learner-owned
profile, device, and observer-relationship resources (Requirement 17.2). Reads
run through the scoped identity service; writes require and apply an
``Idempotency-Key`` (Requirements 17.8-17.10). Owner scope is always derived
server-side, and absence is reported scope-safely (Requirements 17.11, 17.12).
"""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import (
    get_actor,
    get_idempotency_key,
    perform_identity_write,
    read_identity_service,
)
from app.api.schemas import (
    DeviceRegisterRequest,
    DeviceResponse,
    MeResponse,
    ProfileResponse,
    ProfileUpdateRequest,
    RelationshipGrantRequest,
    RelationshipResponse,
)
from app.api.serialization import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, CursorPage, paginate
from app.domain.identity import (
    ActorContext,
    NotFoundError,
    Profile,
    ValidationError,
)
from app.services.identity import IdentityService

router = APIRouter(prefix="/v1", tags=["identity"])

T = TypeVar("T")


def _paginate_or_400(
    records: Sequence[T],
    *,
    key: Callable[[T], str],
    cursor: str | None,
    limit: int,
) -> tuple[list[T], str | None]:
    try:
        return paginate(records, key=key, cursor=cursor, limit=limit)
    except ValueError as error:
        raise ValidationError(str(error), field="cursor") from error


@router.get("/me", operation_id="get_me")
def get_me(
    request: Request,
    actor: ActorContext = Depends(get_actor),
) -> MeResponse:
    reader = read_identity_service(request)

    def _read(service: IdentityService) -> Profile | None:
        try:
            return service.get_profile(actor, request_id=request.state.request_id)
        except NotFoundError:
            return None

    profile = reader.run(_read)
    return MeResponse.from_context(actor, profile)


@router.get("/me/profile", operation_id="get_profile")
def get_profile(
    request: Request,
    actor: ActorContext = Depends(get_actor),
) -> ProfileResponse:
    reader = read_identity_service(request)
    profile = reader.run(
        lambda service: service.get_profile(actor, request_id=request.state.request_id)
    )
    return ProfileResponse.from_domain(profile)


@router.patch("/me/profile", operation_id="update_profile")
def update_profile(
    request: Request,
    body: ProfileUpdateRequest,
    actor: ActorContext = Depends(get_actor),
    idempotency_key: str = Depends(get_idempotency_key),
) -> ProfileResponse:
    def _mutate(service: IdentityService) -> Profile:
        return service.update_profile(
            actor,
            grade_level=body.grade_level,
            timezone=body.timezone,
            study_preferences=body.study_preferences,
            request_id=request.state.request_id,
        )

    profile = perform_identity_write(
        request,
        actor,
        operation="identity.update_profile",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        mutate=_mutate,
        reload=lambda service, _ref: service.get_profile(actor),
        result_ref=lambda profile: profile.user_id,
    )
    return ProfileResponse.from_domain(profile)


@router.get("/me/devices", operation_id="list_devices")
def list_devices(
    request: Request,
    actor: ActorContext = Depends(get_actor),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> CursorPage[DeviceResponse]:
    reader = read_identity_service(request)
    devices = reader.run(
        lambda service: service.list_devices(actor, request_id=request.state.request_id)
    )
    window, next_cursor = _paginate_or_400(
        devices, key=lambda device: str(device.id), cursor=cursor, limit=limit
    )
    return CursorPage[DeviceResponse](
        items=[DeviceResponse.from_domain(device) for device in window],
        next_cursor=next_cursor,
    )


@router.get("/me/devices/{device_id}", operation_id="get_device")
def get_device(
    request: Request,
    device_id: UUID,
    actor: ActorContext = Depends(get_actor),
) -> DeviceResponse:
    reader = read_identity_service(request)
    device = reader.run(
        lambda service: service.get_device(
            actor, device_id, request_id=request.state.request_id
        )
    )
    return DeviceResponse.from_domain(device)


@router.post("/me/devices", operation_id="register_device", status_code=201)
def register_device(
    request: Request,
    body: DeviceRegisterRequest,
    actor: ActorContext = Depends(get_actor),
    idempotency_key: str = Depends(get_idempotency_key),
) -> DeviceResponse:
    device = perform_identity_write(
        request,
        actor,
        operation="identity.register_device",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        mutate=lambda service: service.register_device(
            actor,
            name=body.name,
            platform=body.platform,
            request_id=request.state.request_id,
        ),
        reload=lambda service, ref: service.get_device(actor, ref),
        result_ref=lambda device: device.id,
    )
    return DeviceResponse.from_domain(device)


@router.delete("/me/devices/{device_id}", operation_id="revoke_device")
def revoke_device(
    request: Request,
    device_id: UUID,
    actor: ActorContext = Depends(get_actor),
    idempotency_key: str = Depends(get_idempotency_key),
) -> DeviceResponse:
    device = perform_identity_write(
        request,
        actor,
        operation="identity.revoke_device",
        key=idempotency_key,
        payload={"device_id": str(device_id)},
        mutate=lambda service: service.revoke_device(
            actor, device_id, request_id=request.state.request_id
        ),
        reload=lambda service, ref: service.get_device(actor, ref),
        result_ref=lambda device: device.id,
    )
    return DeviceResponse.from_domain(device)


@router.get("/me/relationships", operation_id="list_relationships")
def list_relationships(
    request: Request,
    actor: ActorContext = Depends(get_actor),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> CursorPage[RelationshipResponse]:
    reader = read_identity_service(request)
    relationships = reader.run(
        lambda service: service.list_relationships(
            actor, request_id=request.state.request_id
        )
    )
    window, next_cursor = _paginate_or_400(
        relationships,
        key=lambda relationship: str(relationship.id),
        cursor=cursor,
        limit=limit,
    )
    return CursorPage[RelationshipResponse](
        items=[RelationshipResponse.from_domain(item) for item in window],
        next_cursor=next_cursor,
    )


@router.get(
    "/me/relationships/{relationship_id}", operation_id="get_relationship"
)
def get_relationship(
    request: Request,
    relationship_id: UUID,
    actor: ActorContext = Depends(get_actor),
) -> RelationshipResponse:
    reader = read_identity_service(request)
    relationship = reader.run(
        lambda service: service.get_relationship(
            actor, relationship_id, request_id=request.state.request_id
        )
    )
    return RelationshipResponse.from_domain(relationship)


@router.post("/me/relationships", operation_id="grant_relationship", status_code=201)
def grant_relationship(
    request: Request,
    body: RelationshipGrantRequest,
    actor: ActorContext = Depends(get_actor),
    idempotency_key: str = Depends(get_idempotency_key),
) -> RelationshipResponse:
    relationship = perform_identity_write(
        request,
        actor,
        operation="identity.grant_relationship",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        mutate=lambda service: service.grant_relationship(
            actor,
            observer_user_id=body.observer_user_id,
            role=body.role,
            permission_scope=set(body.permission_scope),
            expires_at=body.expires_at,
            request_id=request.state.request_id,
        ),
        reload=lambda service, ref: service.get_relationship(actor, ref),
        result_ref=lambda relationship: relationship.id,
    )
    return RelationshipResponse.from_domain(relationship)


@router.delete(
    "/me/relationships/{relationship_id}", operation_id="revoke_relationship"
)
def revoke_relationship(
    request: Request,
    relationship_id: UUID,
    actor: ActorContext = Depends(get_actor),
    idempotency_key: str = Depends(get_idempotency_key),
) -> RelationshipResponse:
    relationship = perform_identity_write(
        request,
        actor,
        operation="identity.revoke_relationship",
        key=idempotency_key,
        payload={"relationship_id": str(relationship_id)},
        mutate=lambda service: service.revoke_relationship(
            actor, relationship_id, request_id=request.state.request_id
        ),
        reload=lambda service, ref: service.get_relationship(actor, ref),
        result_ref=lambda relationship: relationship.id,
    )
    return RelationshipResponse.from_domain(relationship)
