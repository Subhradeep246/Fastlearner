from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, select
from sqlalchemy.exc import IntegrityError

from app.clock import Clock, system_clock
from app.persistence.models import JobStatus, outbox_jobs, require_owner, utc_datetime


@dataclass(frozen=True)
class OutboxEnqueue:
    """Outcome of enqueuing an outbox job.

    ``created`` is false when an existing job already claimed the deterministic
    deduplication key, so committed intent is never duplicated on replay.
    """

    job_id: UUID
    created: bool


class SqlOutboxStore:
    """Transactional outbox: durable intent recorded inside the domain transaction.

    Jobs are written in the same transaction that commits canonical state, so
    queue or worker loss can never erase committed intent. Payloads SHOULD carry
    identifiers rather than full learner content.
    """

    def __init__(self, connection: Connection, clock: Clock = system_clock) -> None:
        self._connection = connection
        self._clock = clock

    def enqueue(
        self,
        *,
        owner_user_id: UUID | None,
        kind: str,
        deduplication_key: str,
        payload: dict[str, Any],
        available_at: datetime | None = None,
    ) -> OutboxEnqueue:
        owner = require_owner(owner_user_id)
        existing_id = self._existing_job_id(kind, deduplication_key)
        if existing_id is not None:
            return OutboxEnqueue(job_id=existing_id, created=False)

        job_id = uuid4()
        available = utc_datetime(available_at) if available_at is not None else self._clock()
        try:
            self._connection.execute(
                outbox_jobs.insert().values(
                    id=job_id,
                    owner_user_id=owner,
                    kind=kind,
                    deduplication_key=deduplication_key,
                    payload=dict(payload),
                    status=JobStatus.PENDING.value,
                    available_at=available,
                    leased_until=None,
                    attempt_count=0,
                    last_error_code=None,
                )
            )
        except IntegrityError:
            duplicate_id = self._existing_job_id(kind, deduplication_key)
            if duplicate_id is not None:
                return OutboxEnqueue(job_id=duplicate_id, created=False)
            raise
        return OutboxEnqueue(job_id=job_id, created=True)

    def _existing_job_id(self, kind: str, deduplication_key: str) -> UUID | None:
        return self._connection.execute(
            select(outbox_jobs.c.id).where(
                outbox_jobs.c.kind == kind,
                outbox_jobs.c.deduplication_key == deduplication_key,
            )
        ).scalar_one_or_none()
