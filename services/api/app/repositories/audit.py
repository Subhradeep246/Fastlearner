from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection

from app.clock import Clock, system_clock
from app.persistence.models import audit_records, require_owner, utc_datetime


class SqlAuditLog:
    """Immutable audit trail for confirmed mutations and denials."""

    def __init__(self, connection: Connection, clock: Clock = system_clock) -> None:
        self._connection = connection
        self._clock = clock

    def record(
        self,
        *,
        owner_user_id: UUID | None,
        actor_user_id: UUID,
        action: str,
        resource_kind: str,
        resource_id: UUID | None = None,
        request_id: str | UUID | None = None,
        outcome: str = "succeeded",
        details: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> UUID:
        """Persist an audit record. Confirmed learner-state changes MUST call this.

        The ``request_id`` is retained inside ``details`` so every audited action
        carries action type, actor, target, time, and request identifier.
        """
        owner = require_owner(owner_user_id)
        payload: dict[str, Any] = dict(details or {})
        if request_id is not None:
            payload.setdefault("request_id", str(request_id))
        audit_id = uuid4()
        self._connection.execute(
            audit_records.insert().values(
                id=audit_id,
                owner_user_id=owner,
                actor_user_id=actor_user_id,
                action=action,
                resource_kind=resource_kind,
                resource_id=resource_id,
                outcome=outcome,
                details=payload,
                occurred_at=utc_datetime(occurred_at) if occurred_at is not None else self._clock(),
            )
        )
        return audit_id
