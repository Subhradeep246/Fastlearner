"""Identity, profile, device, and observer-relationship application service.

The service resolves the effective owner scope from the authenticated identity
and any active relationship, deliberately ignoring owner identifiers supplied by
clients (Requirements 2.10 and 17.11). Learner owners may mutate their own
profile, devices, and relationships; observers receive an authorization error
for any mutation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.domain.identity import (
    ActorContext,
    AuthorizationError,
    Device,
    DeviceStatus,
    GRANTABLE_OBSERVER_SCOPES,
    LEARNER_OWNER_SCOPES,
    Profile,
    Relationship,
    RelationshipRole,
    RelationshipStatus,
    Role,
    ValidationError,
    validate_grade_level,
)
from app.repositories.identity import IdentityRepository


def _now(at: datetime | None = None) -> datetime:
    return at if at is not None else datetime.now(timezone.utc)


class IdentityService:
    """Use cases for learner identity, profile, devices, and relationships."""

    def __init__(self, repository: IdentityRepository) -> None:
        self._repository = repository

    # -- owner scope resolution -------------------------------------------
    def resolve_actor_context(
        self,
        actor_user_id: UUID,
        *,
        session_id: UUID | None = None,
        requested_owner_id: UUID | None = None,  # noqa: ARG002 - intentionally ignored
        at: datetime | None = None,
    ) -> ActorContext:
        """Derive the effective owner scope for an authenticated actor.

        ``requested_owner_id`` is accepted only so callers can pass a client
        value without special-casing; it is never trusted. The owner scope is
        derived from whether the actor is the learner owner or an active
        observer. Absent or inactive relationships yield an authorization error.
        """
        profile = self._repository.get_profile(actor_user_id)
        if profile is not None and profile.owner_user_id == actor_user_id:
            return ActorContext(
                actor_id=actor_user_id,
                owner_id=actor_user_id,
                role=Role.LEARNER,
                scopes=LEARNER_OWNER_SCOPES,
                session_id=session_id,
            )

        relationship = self._repository.find_observer_relationship(actor_user_id)
        if relationship is not None and relationship.is_active(at):
            return ActorContext(
                actor_id=actor_user_id,
                owner_id=relationship.learner_user_id,
                role=relationship.role.as_actor_role(),
                scopes=relationship.effective_scopes(at),
                session_id=session_id,
            )

        raise AuthorizationError()

    # -- profile -----------------------------------------------------------
    def get_profile(self, actor: ActorContext) -> Profile:
        profile = self._repository.get_profile(actor.owner_id)
        if profile is None:
            raise AuthorizationError("No profile is available for the authorized scope.")
        return profile

    def update_profile(
        self,
        actor: ActorContext,
        *,
        grade_level: int | None = None,
        timezone: str | None = None,
        study_preferences: dict[str, Any] | None = None,
    ) -> Profile:
        self._require_owner(actor)
        current = self._repository.get_profile(actor.owner_id)
        if current is None:
            raise AuthorizationError("No profile is available for the authorized scope.")

        next_grade = current.grade_level if grade_level is None else validate_grade_level(grade_level)
        next_timezone = current.timezone if timezone is None else _validate_timezone(timezone)
        next_prefs = current.study_preferences if study_preferences is None else dict(study_preferences)

        updated = replace(
            current,
            grade_level=next_grade,
            timezone=next_timezone,
            study_preferences=next_prefs,
        )
        return self._repository.upsert_profile(updated)

    # -- devices -----------------------------------------------------------
    def list_devices(self, actor: ActorContext) -> list[Device]:
        return self._repository.list_devices(actor.owner_id)

    def register_device(
        self,
        actor: ActorContext,
        *,
        name: str,
        platform: str,
        device_id: UUID | None = None,
        at: datetime | None = None,
    ) -> Device:
        self._require_owner(actor)
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValidationError("Device name is required.", field="name")
        clean_platform = (platform or "").strip()
        if not clean_platform:
            raise ValidationError("Device platform is required.", field="platform")
        device = Device(
            id=device_id or uuid4(),
            owner_user_id=actor.owner_id,
            name=clean_name,
            platform=clean_platform,
            status=DeviceStatus.ACTIVE,
            last_seen_at=_now(at),
        )
        return self._repository.add_device(device)

    def revoke_device(self, actor: ActorContext, device_id: UUID) -> Device:
        self._require_owner(actor)
        device = self._repository.set_device_status(
            actor.owner_id, device_id, DeviceStatus.REVOKED
        )
        if device is None:
            raise AuthorizationError("The requested device is not available in this scope.")
        return device

    # -- relationships -----------------------------------------------------
    def list_relationships(self, actor: ActorContext) -> list[Relationship]:
        self._require_owner(actor)
        return self._repository.list_relationships(actor.owner_id)

    def grant_relationship(
        self,
        actor: ActorContext,
        *,
        observer_user_id: UUID,
        role: RelationshipRole,
        permission_scope: set[str] | frozenset[str],
        expires_at: datetime | None = None,
        relationship_id: UUID | None = None,
    ) -> Relationship:
        self._require_owner(actor)
        if observer_user_id == actor.owner_id:
            raise ValidationError(
                "An observer relationship cannot target the learner owner.",
                field="observer_user_id",
            )
        if not self._repository.user_exists(observer_user_id):
            raise ValidationError("The observer user does not exist.", field="observer_user_id")

        scope = frozenset(permission_scope)
        if not scope:
            raise ValidationError("At least one read scope is required.", field="permission_scope")
        invalid = scope - GRANTABLE_OBSERVER_SCOPES
        if invalid:
            raise ValidationError(
                "Observer relationships may only grant read scopes.",
                field="permission_scope",
            )

        relationship = Relationship(
            id=relationship_id or uuid4(),
            owner_user_id=actor.owner_id,
            learner_user_id=actor.owner_id,
            observer_user_id=observer_user_id,
            role=role,
            permission_scope=scope,
            status=RelationshipStatus.ACTIVE,
            expires_at=expires_at,
        )
        return self._repository.add_relationship(relationship)

    def revoke_relationship(
        self, actor: ActorContext, relationship_id: UUID, *, at: datetime | None = None
    ) -> Relationship:
        self._require_owner(actor)
        relationship = self._repository.revoke_relationship(
            actor.owner_id, relationship_id, _now(at)
        )
        if relationship is None:
            raise AuthorizationError(
                "The requested relationship is not available in this scope."
            )
        return relationship

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _require_owner(actor: ActorContext) -> None:
        """Observers can never mutate learner data (Requirement 2.6)."""
        if not actor.is_owner:
            raise AuthorizationError("This action requires learner ownership.")


def _validate_timezone(value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValidationError("Timezone is required.", field="timezone")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(clean)
    except Exception as error:  # noqa: BLE001 - normalize to a typed validation error
        raise ValidationError("Timezone must be a valid IANA zone.", field="timezone") from error
    return clean
