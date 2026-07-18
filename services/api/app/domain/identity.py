"""Identity, ownership, and observer-access domain model.

This module holds the pure identity value objects, entities, lifecycle
enumerations, scope vocabulary, and typed domain errors used by the identity
and authorization services. It contains no persistence, framework, or provider
dependencies so the rules stay independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class Role(StrEnum):
    """Effective actor role resolved server-side for the current request."""

    LEARNER = "learner"
    PARENT = "parent"
    TEACHER = "teacher"


class RelationshipRole(StrEnum):
    """Observer relationship roles persisted on ``user_relationships``."""

    PARENT = "parent"
    TEACHER = "teacher"

    def as_actor_role(self) -> Role:
        return Role.PARENT if self is RelationshipRole.PARENT else Role.TEACHER


class RelationshipStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class Scope(StrEnum):
    """Read scopes an observer relationship may grant.

    Learner ownership grants the enabled learner actions directly; observer
    relationships only grant the listed ``*:read`` scopes.
    """

    DASHBOARD_READ = "dashboard:read"
    ASSIGNMENTS_READ = "assignments:read"
    LEARNING_READ = "learning:read"
    MEMORY_READ = "memory:read"
    PATHWAYS_READ = "pathways:read"


#: The scopes a learner owner implicitly holds over their own data.
LEARNER_OWNER_SCOPES: frozenset[str] = frozenset(scope.value for scope in Scope)

#: Scopes an observer relationship is permitted to request. Only read scopes
#: are grantable to observers; any other requested scope is rejected.
GRANTABLE_OBSERVER_SCOPES: frozenset[str] = frozenset(scope.value for scope in Scope)


def _now(reference: datetime | None = None) -> datetime:
    return reference if reference is not None else datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActorContext:
    """The authenticated actor and the owner scope resolved for a request.

    ``owner_id`` is always derived server-side from the authenticated identity
    and any active relationship, never from a client-supplied identifier.
    """

    actor_id: UUID
    owner_id: UUID
    role: Role
    scopes: frozenset[str]
    session_id: UUID | None = None

    @property
    def is_owner(self) -> bool:
        """True when the actor is the learner acting on their own data."""
        return self.role is Role.LEARNER and self.actor_id == self.owner_id

    @property
    def is_observer(self) -> bool:
        return self.role in (Role.PARENT, Role.TEACHER)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class Profile:
    """Learner profile: grade level, timezone, and study preferences."""

    user_id: UUID
    owner_user_id: UUID
    grade_level: int
    timezone: str
    study_preferences: dict[str, Any]


@dataclass(frozen=True)
class Device:
    """A registered device for a learner owner."""

    id: UUID
    owner_user_id: UUID
    name: str
    platform: str
    status: DeviceStatus
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class Relationship:
    """A parent/teacher observer relationship with lifecycle status."""

    id: UUID
    owner_user_id: UUID
    learner_user_id: UUID
    observer_user_id: UUID
    role: RelationshipRole
    permission_scope: frozenset[str]
    status: RelationshipStatus
    expires_at: datetime | None = None

    def is_active(self, at: datetime | None = None) -> bool:
        """Return whether the relationship currently grants observer access.

        A relationship is only active when its status is ``active`` and it has
        not passed any configured expiry instant. Inactive, expired, revoked,
        or absent relationships never grant access.
        """
        if self.status is not RelationshipStatus.ACTIVE:
            return False
        if self.expires_at is not None and self.expires_at <= _now(at):
            return False
        return True

    def effective_scopes(self, at: datetime | None = None) -> frozenset[str]:
        """Scopes granted while active; empty otherwise."""
        if not self.is_active(at):
            return frozenset()
        return frozenset(self.permission_scope) & GRANTABLE_OBSERVER_SCOPES


@dataclass(frozen=True)
class SessionRecord:
    """A stored authentication session bound to an actor and owner scope."""

    id: UUID
    owner_user_id: UUID
    actor_user_id: UUID
    status: SessionStatus
    expires_at: datetime
    session_version: int
    revoked_at: datetime | None = None

    def is_valid(self, at: datetime | None = None) -> bool:
        if self.status is not SessionStatus.ACTIVE:
            return False
        if self.revoked_at is not None:
            return False
        return self.expires_at > _now(at)


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class IdentityError(RuntimeError):
    """Base class for typed identity/authorization errors.

    Every error carries a stable ``code`` and a safe, non-disclosing message so
    the API layer can render a typed envelope without leaking learner data or
    cross-owner existence details.
    """

    code = "identity_error"
    retryable = False

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class AuthenticationError(IdentityError):
    """Authentication is absent or invalid."""

    code = "authentication_error"

    def __init__(self, message: str = "Authentication is required.") -> None:
        super().__init__(message)


class AuthorizationError(IdentityError):
    """The caller is authenticated but not authorized for the request."""

    code = "authorization_error"

    def __init__(self, message: str = "You are not authorized to access this resource.") -> None:
        super().__init__(message)


class ValidationError(IdentityError):
    """A field-level validation failure that must not change canonical state."""

    code = "validation_error"

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field

    def safe_payload(self) -> dict[str, Any]:
        payload = super().safe_payload()
        if self.field is not None:
            payload["field"] = self.field
        return payload


def validate_grade_level(grade_level: int) -> int:
    """Grades 3-12 are the supported learner range."""
    if not isinstance(grade_level, int) or isinstance(grade_level, bool):
        raise ValidationError("Grade level must be an integer.", field="grade_level")
    if not 3 <= grade_level <= 12:
        raise ValidationError("Grade level must be between 3 and 12.", field="grade_level")
    return grade_level
