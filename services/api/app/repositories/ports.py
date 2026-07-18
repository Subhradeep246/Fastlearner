from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.repositories.idempotency import IdempotencyClaim
from app.repositories.outbox import OutboxEnqueue


class IdempotencyStore(Protocol):
    """Port for operation-scoped idempotency records."""

    def begin(
        self,
        *,
        owner_user_id: UUID | None,
        operation: str,
        key: str,
        request_hash: str,
        expires_at: datetime | None = None,
    ) -> IdempotencyClaim: ...

    def complete(
        self,
        *,
        owner_user_id: UUID | None,
        operation: str,
        key: str,
        response_status: int | None = None,
        response_body_hash: str | None = None,
        result_ref: UUID | None = None,
    ) -> None: ...


class AuditLog(Protocol):
    """Port for the immutable audit trail."""

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
    ) -> UUID: ...


class OutboxStore(Protocol):
    """Port for the transactional outbox."""

    def enqueue(
        self,
        *,
        owner_user_id: UUID | None,
        kind: str,
        deduplication_key: str,
        payload: dict[str, Any],
        available_at: datetime | None = None,
    ) -> OutboxEnqueue: ...


class UnitOfWork(Protocol):
    """Port for the transactional boundary that binds the workflow repositories."""

    idempotency: IdempotencyStore
    audit: AuditLog
    outbox: OutboxStore

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
