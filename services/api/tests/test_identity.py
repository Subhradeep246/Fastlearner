"""Unit and integration tests for identity, ownership, and observer access.

Covers Requirements 2.1, 2.2, 2.3, 2.4, 2.9, 2.10, and 17.11: account-local
learner, owner/timestamp scoping, profile/device/relationship records, observer
read-only scope, revocation, and server-side owner-scope derivation that ignores
client-supplied owner identifiers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine

from app.auth.identity import (
    LocalAuthForbiddenError,
    LocalIdentityProvider,
    LocalPersona,
)
from app.auth.sessions import SessionSigner
from app.domain.identity import (
    AuthenticationError,
    AuthorizationError,
    Relationship,
    RelationshipRole,
    RelationshipStatus,
    Role,
    ValidationError,
    validate_grade_level,
)
from app.persistence.models import (
    devices,
    metadata,
    profiles,
    sessions,
    user_relationships,
    users,
)
from app.persistence.seeds import (
    LOCAL_LEARNER_ID,
    LOCAL_PARENT_ID,
    LOCAL_TEACHER_ID,
    seed_local_personas,
)
from app.repositories.identity import SqlIdentityRepository
from app.services.identity import IdentityService

IDENTITY_TABLES = [users, profiles, user_relationships, devices, sessions]
OTHER_LEARNER_ID = UUID("00000000-0000-4000-8000-0000000000aa")


def _seeded_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine, tables=IDENTITY_TABLES)
    with engine.begin() as connection:
        seed_local_personas(connection)
    return engine


# ---------------------------------------------------------------------------
# Pure domain rules
# ---------------------------------------------------------------------------


def test_validate_grade_level_bounds() -> None:
    assert validate_grade_level(3) == 3
    assert validate_grade_level(12) == 12
    for bad in (2, 13, 0):
        with pytest.raises(ValidationError):
            validate_grade_level(bad)


def _relationship(status: RelationshipStatus, expires_at: datetime | None = None) -> Relationship:
    return Relationship(
        id=uuid4(),
        owner_user_id=LOCAL_LEARNER_ID,
        learner_user_id=LOCAL_LEARNER_ID,
        observer_user_id=LOCAL_PARENT_ID,
        role=RelationshipRole.PARENT,
        permission_scope=frozenset({"dashboard:read"}),
        status=status,
        expires_at=expires_at,
    )


def test_relationship_is_active_lifecycle() -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert _relationship(RelationshipStatus.ACTIVE).is_active(now) is True
    assert _relationship(RelationshipStatus.REVOKED).is_active(now) is False
    assert _relationship(RelationshipStatus.EXPIRED).is_active(now) is False
    future = now + timedelta(days=1)
    past = now - timedelta(days=1)
    assert _relationship(RelationshipStatus.ACTIVE, future).is_active(now) is True
    assert _relationship(RelationshipStatus.ACTIVE, past).is_active(now) is False


# ---------------------------------------------------------------------------
# Owner-scope resolution (Requirements 2.10, 17.11)
# ---------------------------------------------------------------------------


def test_learner_resolves_to_own_owner_scope() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        actor = service.resolve_actor_context(LOCAL_LEARNER_ID)
        assert actor.role is Role.LEARNER
        assert actor.owner_id == LOCAL_LEARNER_ID
        assert actor.is_owner is True
        assert actor.has_scope("assignments:read")


def test_observer_resolves_to_learner_owner_with_scope() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        assert parent.role is Role.PARENT
        assert parent.owner_id == LOCAL_LEARNER_ID
        assert parent.is_observer is True
        assert parent.has_scope("memory:read")

        teacher = service.resolve_actor_context(LOCAL_TEACHER_ID)
        assert teacher.owner_id == LOCAL_LEARNER_ID
        # Teacher persona is seeded without memory:read.
        assert teacher.has_scope("memory:read") is False


def test_client_supplied_owner_identifier_is_ignored() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        # A malicious client asks to act as a different owner; it is ignored.
        actor = service.resolve_actor_context(
            LOCAL_PARENT_ID, requested_owner_id=OTHER_LEARNER_ID
        )
        assert actor.owner_id == LOCAL_LEARNER_ID
        assert actor.owner_id != OTHER_LEARNER_ID


def test_actor_without_profile_or_relationship_is_unauthorized() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        with pytest.raises(AuthorizationError):
            service.resolve_actor_context(uuid4())


def test_revoked_relationship_ends_observer_scope() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        repo = SqlIdentityRepository(connection)
        service = IdentityService(repo)
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)
        relationships = service.list_relationships(learner)
        parent_rel = next(r for r in relationships if r.observer_user_id == LOCAL_PARENT_ID)
        service.revoke_relationship(learner, parent_rel.id)
        with pytest.raises(AuthorizationError):
            service.resolve_actor_context(LOCAL_PARENT_ID)


# ---------------------------------------------------------------------------
# Profile: learner mutates, observer denied (Requirements 2.3, 2.6)
# ---------------------------------------------------------------------------


def test_learner_updates_profile_and_observer_cannot() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)
        updated = service.update_profile(
            learner, grade_level=6, timezone="America/New_York"
        )
        assert updated.grade_level == 6
        assert updated.timezone == "America/New_York"

        with pytest.raises(ValidationError):
            service.update_profile(learner, grade_level=99)

        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        with pytest.raises(AuthorizationError):
            service.update_profile(parent, grade_level=7)


# ---------------------------------------------------------------------------
# Devices (Requirement 2.4)
# ---------------------------------------------------------------------------


def test_device_registration_lifecycle_owner_only() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)
        device = service.register_device(learner, name="MacBook", platform="macos")
        assert device.owner_user_id == LOCAL_LEARNER_ID
        assert [d.id for d in service.list_devices(learner)] == [device.id]

        revoked = service.revoke_device(learner, device.id)
        assert revoked.status.value == "revoked"

        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        with pytest.raises(AuthorizationError):
            service.register_device(parent, name="Phone", platform="ios")


# ---------------------------------------------------------------------------
# Relationships (Requirement 2.4, 2.9)
# ---------------------------------------------------------------------------


def test_grant_relationship_validates_scope_and_target() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        connection.execute(
            users.insert().values(
                id=OTHER_LEARNER_ID,
                email="observer@local.fastlearner",
                display_name="New Observer",
                status="active",
            )
        )
        service = IdentityService(SqlIdentityRepository(connection))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)

        granted = service.grant_relationship(
            learner,
            observer_user_id=OTHER_LEARNER_ID,
            role=RelationshipRole.TEACHER,
            permission_scope={"dashboard:read", "learning:read"},
        )
        assert granted.status is RelationshipStatus.ACTIVE
        assert granted.learner_user_id == LOCAL_LEARNER_ID

        with pytest.raises(ValidationError):
            service.grant_relationship(
                learner,
                observer_user_id=OTHER_LEARNER_ID,
                role=RelationshipRole.PARENT,
                permission_scope={"assignments:write"},
            )
        with pytest.raises(ValidationError):
            service.grant_relationship(
                learner,
                observer_user_id=uuid4(),
                role=RelationshipRole.PARENT,
                permission_scope={"dashboard:read"},
            )
        with pytest.raises(ValidationError):
            service.grant_relationship(
                learner,
                observer_user_id=LOCAL_LEARNER_ID,
                role=RelationshipRole.PARENT,
                permission_scope={"dashboard:read"},
            )


# ---------------------------------------------------------------------------
# Secure session token contracts
# ---------------------------------------------------------------------------


def test_session_signer_roundtrip_and_tamper_detection() -> None:
    signer = SessionSigner("unit-test-signing-secret")
    session_id, actor, owner = uuid4(), uuid4(), uuid4()
    issued = signer.issue(session_id=session_id, actor_user_id=actor, owner_user_id=owner)
    claims = signer.verify(issued.token)
    assert claims.session_id == session_id
    assert claims.actor_user_id == actor
    assert claims.owner_user_id == owner

    tampered = issued.token[:-2] + ("aa" if not issued.token.endswith("aa") else "bb")
    with pytest.raises(AuthenticationError):
        signer.verify(tampered)

    other = SessionSigner("a-different-secret")
    with pytest.raises(AuthenticationError):
        other.verify(issued.token)


def test_session_signer_rejects_expired_token() -> None:
    signer = SessionSigner("unit-test-signing-secret")
    issued_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    issued = signer.issue(
        session_id=uuid4(),
        actor_user_id=uuid4(),
        owner_user_id=uuid4(),
        ttl=timedelta(minutes=5),
        issued_at=issued_at,
    )
    with pytest.raises(AuthenticationError):
        signer.verify(issued.token, at=issued_at + timedelta(minutes=10))


def test_session_signer_requires_secret() -> None:
    with pytest.raises(ValueError):
        SessionSigner("   ")


# ---------------------------------------------------------------------------
# Local identity provider
# ---------------------------------------------------------------------------


def test_local_provider_authenticates_seeded_personas() -> None:
    engine = _seeded_engine()
    signer = SessionSigner("unit-test-signing-secret")
    provider = LocalIdentityProvider(signer, environment="development")
    with engine.begin() as connection:
        repo = SqlIdentityRepository(connection)
        issued = provider.issue_local_session(repo, LocalPersona.PARENT)
        actor = provider.authenticate(repo, issued.token)
        assert actor.role is Role.PARENT
        assert actor.owner_id == LOCAL_LEARNER_ID
        assert actor.session_id == issued.claims.session_id


def test_local_provider_rejects_revoked_session() -> None:
    engine = _seeded_engine()
    signer = SessionSigner("unit-test-signing-secret")
    provider = LocalIdentityProvider(signer, environment="development")
    with engine.begin() as connection:
        repo = SqlIdentityRepository(connection)
        issued = provider.issue_local_session(repo, LocalPersona.LEARNER)
        repo.revoke_session(issued.claims.session_id, datetime.now(timezone.utc))
        with pytest.raises(AuthenticationError):
            provider.authenticate(repo, issued.token)


def test_local_provider_forbidden_in_production() -> None:
    signer = SessionSigner("unit-test-signing-secret")
    with pytest.raises(LocalAuthForbiddenError):
        LocalIdentityProvider(signer, environment="production")
