"""Identity repository port and SQLAlchemy Core implementation.

The port exposes the reads and writes the identity/authorization services need:
loading users, profiles, devices, sessions, and observer relationships. Every
owner-scoped lookup requires the resolved owner id as a positional argument.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, and_, select

from app.domain.identity import (
    Device,
    DeviceStatus,
    Profile,
    Relationship,
    RelationshipRole,
    RelationshipStatus,
    SessionRecord,
    SessionStatus,
)
from app.persistence.models import (
    devices,
    profiles,
    sessions,
    user_relationships,
    users,
)


class IdentityRepository(Protocol):
    """Port for identity persistence used by the identity/auth services."""

    def user_exists(self, user_id: UUID) -> bool: ...

    def get_profile(self, owner_user_id: UUID) -> Profile | None: ...

    def upsert_profile(self, profile: Profile) -> Profile: ...

    def list_devices(self, owner_user_id: UUID) -> list[Device]: ...

    def get_device(self, owner_user_id: UUID, device_id: UUID) -> Device | None: ...

    def add_device(self, device: Device) -> Device: ...

    def set_device_status(
        self, owner_user_id: UUID, device_id: UUID, status: DeviceStatus
    ) -> Device | None: ...

    def list_relationships(self, learner_user_id: UUID) -> list[Relationship]: ...

    def get_relationship(
        self, learner_user_id: UUID, relationship_id: UUID
    ) -> Relationship | None: ...

    def find_observer_relationship(
        self, observer_user_id: UUID
    ) -> Relationship | None: ...

    def add_relationship(self, relationship: Relationship) -> Relationship: ...

    def revoke_relationship(
        self, learner_user_id: UUID, relationship_id: UUID, at: datetime
    ) -> Relationship | None: ...

    def get_session(self, session_id: UUID) -> SessionRecord | None: ...

    def add_session(self, record: SessionRecord, token_hash: bytes) -> SessionRecord: ...

    def revoke_session(self, session_id: UUID, at: datetime) -> SessionRecord | None: ...


def _relationship_from_row(row: Any) -> Relationship:
    return Relationship(
        id=row.id,
        owner_user_id=row.owner_user_id,
        learner_user_id=row.learner_user_id,
        observer_user_id=row.observer_user_id,
        role=RelationshipRole(row.role),
        permission_scope=frozenset(row.permission_scope or ()),
        status=RelationshipStatus(row.status),
        expires_at=_as_utc(row.expires_at),
    )


def _device_from_row(row: Any) -> Device:
    return Device(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        platform=row.platform,
        status=DeviceStatus(row.status),
        last_seen_at=_as_utc(row.last_seen_at),
    )


def _session_from_row(row: Any) -> SessionRecord:
    expires_at = _as_utc(row.expires_at)
    assert expires_at is not None  # sessions.expires_at is NOT NULL
    return SessionRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        actor_user_id=row.actor_user_id,
        status=SessionStatus(row.status),
        expires_at=expires_at,
        session_version=row.session_version,
        revoked_at=_as_utc(row.revoked_at),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize DB timestamps to timezone-aware UTC (SQLite returns naive)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SqlIdentityRepository(IdentityRepository):
    """SQLAlchemy Core implementation backed by a live connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    # -- users -------------------------------------------------------------
    def user_exists(self, user_id: UUID) -> bool:
        return (
            self._connection.execute(
                select(users.c.id).where(users.c.id == user_id)
            ).first()
            is not None
        )

    # -- profiles ----------------------------------------------------------
    def get_profile(self, owner_user_id: UUID) -> Profile | None:
        row = self._connection.execute(
            select(profiles).where(profiles.c.owner_user_id == owner_user_id)
        ).first()
        if row is None:
            return None
        return Profile(
            user_id=row.user_id,
            owner_user_id=row.owner_user_id,
            grade_level=row.grade_level,
            timezone=row.timezone,
            study_preferences=dict(row.study_preferences or {}),
        )

    def upsert_profile(self, profile: Profile) -> Profile:
        exists = self._connection.execute(
            select(profiles.c.user_id).where(profiles.c.user_id == profile.user_id)
        ).first()
        values = {
            "owner_user_id": profile.owner_user_id,
            "grade_level": profile.grade_level,
            "timezone": profile.timezone,
            "study_preferences": dict(profile.study_preferences),
            "updated_at": datetime.now(timezone.utc),
        }
        if exists is None:
            self._connection.execute(
                profiles.insert().values(user_id=profile.user_id, **values)
            )
        else:
            self._connection.execute(
                profiles.update()
                .where(profiles.c.user_id == profile.user_id)
                .values(**values)
            )
        loaded = self.get_profile(profile.owner_user_id)
        assert loaded is not None
        return loaded

    # -- devices -----------------------------------------------------------
    def list_devices(self, owner_user_id: UUID) -> list[Device]:
        rows = self._connection.execute(
            select(devices).where(devices.c.owner_user_id == owner_user_id)
        ).all()
        return [_device_from_row(row) for row in rows]

    def get_device(self, owner_user_id: UUID, device_id: UUID) -> Device | None:
        row = self._connection.execute(
            select(devices).where(
                and_(devices.c.owner_user_id == owner_user_id, devices.c.id == device_id)
            )
        ).first()
        return _device_from_row(row) if row is not None else None

    def add_device(self, device: Device) -> Device:
        self._connection.execute(
            devices.insert().values(
                id=device.id,
                owner_user_id=device.owner_user_id,
                name=device.name,
                platform=device.platform,
                status=device.status.value,
                last_seen_at=device.last_seen_at,
            )
        )
        stored = self.get_device(device.owner_user_id, device.id)
        assert stored is not None
        return stored

    def set_device_status(
        self, owner_user_id: UUID, device_id: UUID, status: DeviceStatus
    ) -> Device | None:
        result = self._connection.execute(
            devices.update()
            .where(and_(devices.c.owner_user_id == owner_user_id, devices.c.id == device_id))
            .values(status=status.value, updated_at=datetime.now(timezone.utc))
        )
        if result.rowcount == 0:
            return None
        return self.get_device(owner_user_id, device_id)

    # -- relationships -----------------------------------------------------
    def list_relationships(self, learner_user_id: UUID) -> list[Relationship]:
        rows = self._connection.execute(
            select(user_relationships).where(
                user_relationships.c.learner_user_id == learner_user_id
            )
        ).all()
        return [_relationship_from_row(row) for row in rows]

    def get_relationship(
        self, learner_user_id: UUID, relationship_id: UUID
    ) -> Relationship | None:
        row = self._connection.execute(
            select(user_relationships).where(
                and_(
                    user_relationships.c.learner_user_id == learner_user_id,
                    user_relationships.c.id == relationship_id,
                )
            )
        ).first()
        return _relationship_from_row(row) if row is not None else None

    def find_observer_relationship(self, observer_user_id: UUID) -> Relationship | None:
        row = self._connection.execute(
            select(user_relationships)
            .where(
                and_(
                    user_relationships.c.observer_user_id == observer_user_id,
                    user_relationships.c.status == RelationshipStatus.ACTIVE.value,
                )
            )
            .order_by(user_relationships.c.created_at.desc())
        ).first()
        return _relationship_from_row(row) if row is not None else None

    def add_relationship(self, relationship: Relationship) -> Relationship:
        self._connection.execute(
            user_relationships.insert().values(
                id=relationship.id,
                owner_user_id=relationship.owner_user_id,
                learner_user_id=relationship.learner_user_id,
                observer_user_id=relationship.observer_user_id,
                role=relationship.role.value,
                permission_scope=sorted(relationship.permission_scope),
                status=relationship.status.value,
                expires_at=relationship.expires_at,
            )
        )
        stored = self.get_relationship(relationship.learner_user_id, relationship.id)
        assert stored is not None
        return stored

    def revoke_relationship(
        self, learner_user_id: UUID, relationship_id: UUID, at: datetime
    ) -> Relationship | None:
        result = self._connection.execute(
            user_relationships.update()
            .where(
                and_(
                    user_relationships.c.learner_user_id == learner_user_id,
                    user_relationships.c.id == relationship_id,
                )
            )
            .values(status=RelationshipStatus.REVOKED.value, updated_at=at)
        )
        if result.rowcount == 0:
            return None
        return self.get_relationship(learner_user_id, relationship_id)

    # -- sessions ----------------------------------------------------------
    def get_session(self, session_id: UUID) -> SessionRecord | None:
        row = self._connection.execute(
            select(sessions).where(sessions.c.id == session_id)
        ).first()
        return _session_from_row(row) if row is not None else None

    def add_session(self, record: SessionRecord, token_hash: bytes) -> SessionRecord:
        self._connection.execute(
            sessions.insert().values(
                id=record.id,
                owner_user_id=record.owner_user_id,
                actor_user_id=record.actor_user_id,
                token_hash=token_hash,
                status=record.status.value,
                expires_at=record.expires_at,
                session_version=record.session_version,
            )
        )
        stored = self.get_session(record.id)
        assert stored is not None
        return stored

    def revoke_session(self, session_id: UUID, at: datetime) -> SessionRecord | None:
        result = self._connection.execute(
            sessions.update()
            .where(sessions.c.id == session_id)
            .values(status=SessionStatus.REVOKED.value, revoked_at=at, updated_at=at)
        )
        if result.rowcount == 0:
            return None
        return self.get_session(session_id)
