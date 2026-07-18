"""Identity provider abstraction and local development implementation.

``IdentityProvider`` authenticates an inbound bearer token to an
:class:`ActorContext`. ``LocalIdentityProvider`` seeds a fixed learner and
optional parent/teacher personas, issues short-lived signed development
sessions, binds only to loopback, and is forbidden when the environment is
production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from app.auth.sessions import DEFAULT_SESSION_TTL, IssuedSession, SessionSigner
from app.domain.identity import (
    ActorContext,
    AuthenticationError,
    SessionRecord,
    SessionStatus,
)
from app.repositories.identity import IdentityRepository
from app.services.identity import IdentityService
from app.persistence.seeds import (
    LOCAL_LEARNER_ID,
    LOCAL_PARENT_ID,
    LOCAL_TEACHER_ID,
)


class LocalPersona(StrEnum):
    """Stable loopback-only development identities seeded by the seed command."""

    LEARNER = "learner"
    PARENT = "parent"
    TEACHER = "teacher"

    @property
    def user_id(self) -> UUID:
        return _PERSONA_IDS[self]


_PERSONA_IDS: dict[LocalPersona, UUID] = {
    LocalPersona.LEARNER: LOCAL_LEARNER_ID,
    LocalPersona.PARENT: LOCAL_PARENT_ID,
    LocalPersona.TEACHER: LOCAL_TEACHER_ID,
}


class LocalAuthForbiddenError(RuntimeError):
    """Local auth mode is not permitted in production."""

    code = "local_auth_forbidden"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Local development authentication is unavailable in production.")


class IdentityProvider(Protocol):
    """Authenticate a bearer token into an effective actor context."""

    def authenticate(
        self,
        repository: IdentityRepository,
        token: str,
        *,
        at: datetime | None = None,
    ) -> ActorContext: ...


class LocalIdentityProvider(IdentityProvider):
    """Loopback-only development identity provider.

    Sessions are signed with the server-side signing secret and verified
    against a stored session record so revocation, expiry, and session-version
    bumps end access immediately.
    """

    def __init__(self, signer: SessionSigner, *, environment: str = "development") -> None:
        if environment == "production":
            raise LocalAuthForbiddenError()
        self._signer = signer
        self._environment = environment

    def issue_local_session(
        self,
        repository: IdentityRepository,
        persona: LocalPersona,
        *,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        issued_at: datetime | None = None,
        service: IdentityService | None = None,
    ) -> IssuedSession:
        """Create a persisted development session token for a seeded persona."""
        if self._environment == "production":
            raise LocalAuthForbiddenError()

        actor_user_id = persona.user_id
        if not repository.user_exists(actor_user_id):
            raise AuthenticationError(
                "The requested local persona has not been seeded."
            )

        resolver = service or IdentityService(repository)
        context = resolver.resolve_actor_context(actor_user_id, at=issued_at)

        now = issued_at or datetime.now(timezone.utc)
        session_id = uuid4()
        issued = self._signer.issue(
            session_id=session_id,
            actor_user_id=actor_user_id,
            owner_user_id=context.owner_id,
            session_version=1,
            ttl=ttl,
            issued_at=now,
        )
        record = SessionRecord(
            id=session_id,
            owner_user_id=context.owner_id,
            actor_user_id=actor_user_id,
            status=SessionStatus.ACTIVE,
            expires_at=issued.claims.expires_at,
            session_version=1,
        )
        repository.add_session(record, issued.token_hash)
        return issued

    def authenticate(
        self,
        repository: IdentityRepository,
        token: str,
        *,
        at: datetime | None = None,
    ) -> ActorContext:
        claims = self._signer.verify(token, at=at)
        record = repository.get_session(claims.session_id)
        if record is None:
            raise AuthenticationError("Session is not recognized.")
        if not record.is_valid(at):
            raise AuthenticationError("Session is no longer active.")
        if record.session_version != claims.session_version:
            raise AuthenticationError("Session has been superseded.")
        if record.actor_user_id != claims.actor_user_id:
            raise AuthenticationError("Session actor mismatch.")

        # Re-resolve owner scope from current relationship state so revocation
        # of an observer relationship ends access on the next request.
        return IdentityService(repository).resolve_actor_context(
            record.actor_user_id,
            session_id=record.id,
            at=at,
        )
