"""Request and response payloads for the authenticated identity endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.api.serialization import ApiModel
from app.domain.identity import (
    ActorContext,
    Device,
    Profile,
    Relationship,
    RelationshipRole,
)


class ProfileResponse(ApiModel):
    user_id: UUID
    owner_user_id: UUID
    grade_level: int
    timezone: str
    study_preferences: dict[str, Any]

    @classmethod
    def from_domain(cls, profile: Profile) -> "ProfileResponse":
        return cls(
            user_id=profile.user_id,
            owner_user_id=profile.owner_user_id,
            grade_level=profile.grade_level,
            timezone=profile.timezone,
            study_preferences=dict(profile.study_preferences),
        )


class MeResponse(ApiModel):
    """The current authenticated actor and resolved owner scope."""

    actor_id: UUID
    owner_id: UUID
    role: str
    scopes: list[str]
    is_owner: bool
    is_observer: bool
    profile: ProfileResponse | None = None

    @classmethod
    def from_context(
        cls, actor: ActorContext, profile: Profile | None
    ) -> "MeResponse":
        return cls(
            actor_id=actor.actor_id,
            owner_id=actor.owner_id,
            role=str(actor.role),
            scopes=sorted(actor.scopes),
            is_owner=actor.is_owner,
            is_observer=actor.is_observer,
            profile=ProfileResponse.from_domain(profile) if profile is not None else None,
        )


class ProfileUpdateRequest(ApiModel):
    grade_level: int | None = None
    timezone: str | None = None
    study_preferences: dict[str, Any] | None = None


class DeviceResponse(ApiModel):
    id: UUID
    owner_user_id: UUID
    name: str
    platform: str
    status: str
    last_seen_at: datetime | None = None

    @classmethod
    def from_domain(cls, device: Device) -> "DeviceResponse":
        return cls(
            id=device.id,
            owner_user_id=device.owner_user_id,
            name=device.name,
            platform=device.platform,
            status=str(device.status),
            last_seen_at=device.last_seen_at,
        )


class DeviceRegisterRequest(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=24)


class RelationshipResponse(ApiModel):
    id: UUID
    owner_user_id: UUID
    learner_user_id: UUID
    observer_user_id: UUID
    role: str
    permission_scope: list[str]
    status: str
    expires_at: datetime | None = None

    @classmethod
    def from_domain(cls, relationship: Relationship) -> "RelationshipResponse":
        return cls(
            id=relationship.id,
            owner_user_id=relationship.owner_user_id,
            learner_user_id=relationship.learner_user_id,
            observer_user_id=relationship.observer_user_id,
            role=str(relationship.role),
            permission_scope=sorted(relationship.permission_scope),
            status=str(relationship.status),
            expires_at=relationship.expires_at,
        )


class RelationshipGrantRequest(ApiModel):
    observer_user_id: UUID
    role: RelationshipRole
    permission_scope: list[str] = Field(min_length=1)
    expires_at: datetime | None = None
