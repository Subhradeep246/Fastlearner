from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, select
from sqlalchemy.exc import IntegrityError

from app.clock import Clock, system_clock
from app.persistence.models import idempotency_records, require_owner, utc_datetime
from app.repositories.errors import (
    IdempotencyInProgress,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)


@dataclass(frozen=True)
class IdempotentOutcome:
    """The recorded outcome of a completed operation, replayed on repeat keys."""

    response_status: int | None
    response_body_hash: str | None
    result_ref: UUID | None


@dataclass(frozen=True)
class IdempotencyClaim:
    """Result of claiming an operation-scoped idempotency key.

    ``is_new`` indicates a fresh claim that should proceed to perform the
    mutation. When ``completed`` is true, ``outcome`` carries the original
    result and the caller MUST return it without repeating the mutation.
    """

    is_new: bool
    completed: bool
    outcome: IdempotentOutcome | None


def hash_request(payload: Any) -> str:
    """Deterministically hash a request body for idempotency-key binding."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SqlIdempotencyStore:
    """Operation-scoped idempotency records keyed by ``(owner, operation, key)``."""

    def __init__(self, connection: Connection, clock: Clock = system_clock) -> None:
        self._connection = connection
        self._clock = clock

    def begin(
        self,
        *,
        owner_user_id: UUID | None,
        operation: str,
        key: str,
        request_hash: str,
        expires_at: datetime | None = None,
    ) -> IdempotencyClaim:
        owner = require_owner(owner_user_id)
        if not key:
            raise MissingIdempotencyKey(operation)

        existing = self._read(owner, operation, key)
        if existing is not None:
            return self._claim_from_existing(operation, key, request_hash, existing)

        try:
            self._connection.execute(
                idempotency_records.insert().values(
                    id=uuid4(),
                    owner_user_id=owner,
                    operation=operation,
                    key=key,
                    request_hash=request_hash,
                    response_status=None,
                    response_body_hash=None,
                    result_ref=None,
                    expires_at=utc_datetime(expires_at) if expires_at is not None else None,
                )
            )
        except IntegrityError as error:
            # A concurrent request won the unique constraint race.
            raise IdempotencyInProgress(operation, key) from error
        return IdempotencyClaim(is_new=True, completed=False, outcome=None)

    def complete(
        self,
        *,
        owner_user_id: UUID | None,
        operation: str,
        key: str,
        response_status: int | None = None,
        response_body_hash: str | None = None,
        result_ref: UUID | None = None,
    ) -> None:
        owner = require_owner(owner_user_id)
        self._connection.execute(
            idempotency_records.update()
            .where(
                idempotency_records.c.owner_user_id == owner,
                idempotency_records.c.operation == operation,
                idempotency_records.c.key == key,
            )
            .values(
                response_status=response_status,
                response_body_hash=response_body_hash,
                result_ref=result_ref,
            )
        )

    def _read(self, owner: UUID, operation: str, key: str) -> Any:
        return self._connection.execute(
            select(idempotency_records).where(
                idempotency_records.c.owner_user_id == owner,
                idempotency_records.c.operation == operation,
                idempotency_records.c.key == key,
            )
        ).mappings().first()

    @staticmethod
    def _claim_from_existing(operation: str, key: str, request_hash: str, existing: Any) -> IdempotencyClaim:
        if existing["request_hash"] != request_hash:
            raise IdempotencyKeyConflict(operation, key)
        if existing["response_status"] is None and existing["result_ref"] is None:
            raise IdempotencyInProgress(operation, key)
        return IdempotencyClaim(
            is_new=False,
            completed=True,
            outcome=IdempotentOutcome(
                response_status=existing["response_status"],
                response_body_hash=existing["response_body_hash"],
                result_ref=existing["result_ref"],
            ),
        )
