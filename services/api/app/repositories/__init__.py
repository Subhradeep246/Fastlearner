"""Repository ports and synchronous SQLAlchemy implementations.

This package provides the durable workflow primitives shared across domain
services: a transactional unit of work, operation-scoped idempotency records,
an audit trail, and a transactional outbox.
"""

from app.repositories.audit import SqlAuditLog
from app.repositories.errors import (
    IdempotencyInProgress,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
    UnknownJobKind,
    WorkflowError,
)
from app.repositories.idempotency import (
    IdempotencyClaim,
    IdempotentOutcome,
    SqlIdempotencyStore,
    hash_request,
)
from app.repositories.outbox import OutboxEnqueue, SqlOutboxStore
from app.repositories.ports import AuditLog, IdempotencyStore, OutboxStore, UnitOfWork
from app.repositories.unit_of_work import SqlUnitOfWork, unit_of_work

__all__ = [
    "AuditLog",
    "IdempotencyClaim",
    "IdempotencyInProgress",
    "IdempotencyKeyConflict",
    "IdempotencyStore",
    "IdempotentOutcome",
    "MissingIdempotencyKey",
    "OutboxEnqueue",
    "OutboxStore",
    "SqlAuditLog",
    "SqlIdempotencyStore",
    "SqlOutboxStore",
    "SqlUnitOfWork",
    "UnitOfWork",
    "UnknownJobKind",
    "WorkflowError",
    "hash_request",
    "unit_of_work",
]
