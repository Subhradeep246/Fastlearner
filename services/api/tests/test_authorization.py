"""Tests for centralized policy enforcement and owner-scoped repositories.

Covers Requirements 2.5, 2.6, 2.7, 2.8, 2.9, 6.14, 17.12, 17.15, 19.9, 19.10,
and 24.8: active relationship scope intersection, observer read-only denial,
inactive/absent/out-of-scope denial, revocation ending access, scope-safe
absence that never discloses cross-owner existence, pseudonymous denial
auditing, and a mandatory owner predicate on every learner-data query.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine

from app.auth.policy import (
    AccessMode,
    LoggingDenialSink,
    NullDenialSink,
    PolicyDenial,
    PolicyEngine,
    ResourceKind,
    pseudonymize,
)
from app.domain.identity import (
    ActorContext,
    AuthorizationError,
    NotFoundError,
    Role,
    Scope,
)
from app.persistence.models import devices, metadata, profiles, sessions, user_relationships, users
from app.persistence.seeds import (
    LOCAL_LEARNER_ID,
    LOCAL_PARENT_ID,
    LOCAL_TEACHER_ID,
    seed_local_personas,
)
from app.repositories.identity import SqlIdentityRepository
from app.repositories.scoping import owner_predicate, owner_scoped_select
from app.services.identity import IdentityService

IDENTITY_TABLES = [users, profiles, user_relationships, devices, sessions]
OTHER_LEARNER_ID = UUID("00000000-0000-4000-8000-0000000000cc")


class RecordingDenialSink(NullDenialSink):
    """Captures denial records so tests can assert on pseudonymous content."""

    def __init__(self) -> None:
        self.records: list[PolicyDenial] = []

    def record_denial(self, denial: PolicyDenial) -> None:
        self.records.append(denial)


def _seeded_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine, tables=IDENTITY_TABLES)
    with engine.begin() as connection:
        seed_local_personas(connection)
    return engine


def _owner_actor() -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_LEARNER_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.LEARNER,
        scopes=frozenset(scope.value for scope in Scope),
    )


def _observer_actor(scopes: set[str]) -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_PARENT_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.PARENT,
        scopes=frozenset(scopes),
    )


# ---------------------------------------------------------------------------
# PolicyEngine: ownership, scope intersection, read-only (2.5, 2.6, 2.7)
# ---------------------------------------------------------------------------


def test_owner_is_authorized_for_reads_and_writes() -> None:
    engine = PolicyEngine(NullDenialSink())
    actor = _owner_actor()
    for kind in ResourceKind:
        engine.authorize(actor, AccessMode.READ, kind)
        engine.authorize(actor, AccessMode.WRITE, kind)


def test_observer_read_allowed_only_within_granted_scope() -> None:
    engine = PolicyEngine(NullDenialSink())
    observer = _observer_actor({Scope.DASHBOARD_READ.value})
    # Granted scope -> allowed.
    engine.authorize(observer, AccessMode.READ, ResourceKind.DASHBOARD)
    # Not-granted scope -> denied (Requirement 2.7 out-of-scope).
    with pytest.raises(AuthorizationError):
        engine.authorize(observer, AccessMode.READ, ResourceKind.MEMORY)


def test_observer_write_is_always_denied() -> None:
    engine = PolicyEngine(NullDenialSink())
    observer = _observer_actor({scope.value for scope in Scope})
    for kind in ResourceKind:
        with pytest.raises(AuthorizationError):
            engine.authorize(observer, AccessMode.WRITE, kind)


def test_observer_cannot_read_owner_only_resources() -> None:
    engine = PolicyEngine(NullDenialSink())
    observer = _observer_actor({scope.value for scope in Scope})
    for kind in (ResourceKind.DEVICE, ResourceKind.RELATIONSHIP):
        with pytest.raises(AuthorizationError):
            engine.authorize(observer, AccessMode.READ, kind)


def test_inactive_relationship_has_no_scopes_and_is_denied() -> None:
    # A revoked/expired relationship resolves to an empty scope set; every
    # scoped read is denied here (Requirement 2.7 inactive/expired).
    engine = PolicyEngine(NullDenialSink())
    observer = _observer_actor(set())
    with pytest.raises(AuthorizationError):
        engine.authorize(observer, AccessMode.READ, ResourceKind.DASHBOARD)


def test_unknown_role_actor_is_denied() -> None:
    engine = PolicyEngine(NullDenialSink())
    stranger = ActorContext(
        actor_id=uuid4(), owner_id=uuid4(), role=Role.LEARNER, scopes=frozenset()
    )
    # Not the owner (actor_id != owner_id) and not an observer role.
    with pytest.raises(AuthorizationError):
        engine.authorize(stranger, AccessMode.READ, ResourceKind.DASHBOARD)


# ---------------------------------------------------------------------------
# Pseudonymous denial auditing (Requirements 19.10, 21.5)
# ---------------------------------------------------------------------------


def test_denials_are_recorded_pseudonymously_without_protected_data() -> None:
    sink = RecordingDenialSink()
    engine = PolicyEngine(sink)
    observer = _observer_actor({Scope.DASHBOARD_READ.value})
    with pytest.raises(AuthorizationError):
        engine.authorize(
            observer, AccessMode.READ, ResourceKind.MEMORY, request_id="req-123"
        )
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.decision == "denied"
    assert record.reason_code == "scope_not_granted"
    assert record.resource_kind == "memory"
    assert record.action == "read"
    assert record.request_id == "req-123"
    # The raw actor UUID must not appear; the pseudonym is a stable digest.
    assert record.actor_pseudonym == pseudonymize(LOCAL_PARENT_ID)
    assert str(LOCAL_PARENT_ID) not in record.actor_pseudonym


def test_pseudonymize_is_stable_and_non_reversible() -> None:
    assert pseudonymize(LOCAL_PARENT_ID) == pseudonymize(LOCAL_PARENT_ID)
    assert pseudonymize(LOCAL_PARENT_ID) != pseudonymize(LOCAL_TEACHER_ID)
    assert str(LOCAL_PARENT_ID) not in pseudonymize(LOCAL_PARENT_ID)


def test_successful_authorization_records_no_denial() -> None:
    sink = RecordingDenialSink()
    engine = PolicyEngine(sink)
    engine.authorize(_owner_actor(), AccessMode.WRITE, ResourceKind.PROFILE)
    assert sink.records == []


# ---------------------------------------------------------------------------
# Owner-scoped repository predicate (Requirement 19.9)
# ---------------------------------------------------------------------------


def test_owner_predicate_requires_owner_scope() -> None:
    with pytest.raises(ValueError):
        owner_predicate(profiles, None)


def test_owner_predicate_rejects_non_owner_scoped_table() -> None:
    with pytest.raises(KeyError):
        owner_predicate(users, LOCAL_LEARNER_ID)


def test_owner_scoped_select_always_filters_owner() -> None:
    stmt = owner_scoped_select(devices, LOCAL_LEARNER_ID, devices.c.id == uuid4())
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "owner_user_id" in compiled


def test_service_reads_never_cross_owner_scope() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        # A device owned by another learner must be invisible to our owner.
        connection.execute(
            users.insert().values(
                id=OTHER_LEARNER_ID,
                email="other@local.fastlearner",
                display_name="Other",
                status="active",
            )
        )
        foreign_device_id = uuid4()
        connection.execute(
            devices.insert().values(
                id=foreign_device_id,
                owner_user_id=OTHER_LEARNER_ID,
                name="Foreign",
                platform="macos",
                status="active",
            )
        )
        service = IdentityService(SqlIdentityRepository(connection), PolicyEngine(NullDenialSink()))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)
        assert service.list_devices(learner) == []
        # Scope-safe absence: a foreign device reads as plain not-found.
        with pytest.raises(NotFoundError):
            service.get_device(learner, foreign_device_id)


# ---------------------------------------------------------------------------
# Scope-safe absence: same not-found shape regardless of owner (17.12)
# ---------------------------------------------------------------------------


def test_absent_and_foreign_resources_are_indistinguishable() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        connection.execute(
            users.insert().values(
                id=OTHER_LEARNER_ID,
                email="other2@local.fastlearner",
                display_name="Other2",
                status="active",
            )
        )
        foreign_rel_id = uuid4()
        connection.execute(
            user_relationships.insert().values(
                id=foreign_rel_id,
                owner_user_id=OTHER_LEARNER_ID,
                learner_user_id=OTHER_LEARNER_ID,
                observer_user_id=LOCAL_PARENT_ID,
                role="parent",
                permission_scope=["dashboard:read"],
                status="active",
            )
        )
        service = IdentityService(SqlIdentityRepository(connection), PolicyEngine(NullDenialSink()))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)

        with pytest.raises(NotFoundError) as truly_absent:
            service.get_relationship(learner, uuid4())
        with pytest.raises(NotFoundError) as foreign_owned:
            service.get_relationship(learner, foreign_rel_id)
        # Identical code and message: cross-owner existence is never disclosed.
        assert truly_absent.value.code == foreign_owned.value.code == "not_found"
        assert str(truly_absent.value) == str(foreign_owned.value)


# ---------------------------------------------------------------------------
# Authorization runs before body-driven lookups / mutation (17.15, 2.6)
# ---------------------------------------------------------------------------


def test_observer_write_denied_before_lookup() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        sink = RecordingDenialSink()
        service = IdentityService(SqlIdentityRepository(connection), PolicyEngine(sink))
        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        # Revoking a non-existent relationship id: authorization must fire first,
        # so we get AuthorizationError (not NotFoundError from a lookup).
        with pytest.raises(AuthorizationError):
            service.revoke_relationship(parent, uuid4())
        assert sink.records[-1].reason_code == "observer_read_only"


def test_observer_reads_within_scope_through_service() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection), PolicyEngine(NullDenialSink()))
        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        # Parent persona holds dashboard:read -> profile read is permitted.
        profile = service.get_profile(parent)
        assert profile.owner_user_id == LOCAL_LEARNER_ID
        # Devices are owner-only; the observer is denied.
        with pytest.raises(AuthorizationError):
            service.list_devices(parent)


# ---------------------------------------------------------------------------
# Revocation ends subsequent access (Requirement 2.9)
# ---------------------------------------------------------------------------


def test_revocation_ends_observer_authorization() -> None:
    engine = _seeded_engine()
    with engine.begin() as connection:
        service = IdentityService(SqlIdentityRepository(connection), PolicyEngine(NullDenialSink()))
        learner = service.resolve_actor_context(LOCAL_LEARNER_ID)
        parent_rel = next(
            r for r in service.list_relationships(learner) if r.observer_user_id == LOCAL_PARENT_ID
        )
        # Before revocation the observer resolves with scopes and can read.
        parent = service.resolve_actor_context(LOCAL_PARENT_ID)
        service.get_profile(parent)

        service.revoke_relationship(learner, parent_rel.id)
        # Re-resolution now fails: the revoked relationship grants no access.
        with pytest.raises(AuthorizationError):
            service.resolve_actor_context(LOCAL_PARENT_ID)


def test_logging_denial_sink_is_safe_default() -> None:
    # The default sink must accept a record without raising and without needing
    # any protected data beyond the pseudonymous fields.
    sink = LoggingDenialSink()
    sink.record_denial(
        PolicyDenial(
            actor_pseudonym=pseudonymize(LOCAL_PARENT_ID),
            resource_kind="memory",
            action="read",
            decision="denied",
            reason_code="scope_not_granted",
            request_id="req-9",
        )
    )
