from __future__ import annotations

from types import TracebackType

from sqlalchemy import Connection, Engine

from app.clock import Clock, system_clock
from app.repositories.audit import SqlAuditLog
from app.repositories.idempotency import SqlIdempotencyStore
from app.repositories.outbox import SqlOutboxStore


class SqlUnitOfWork:
    """Synchronous transactional boundary binding the workflow repositories.

    All work performed through a unit of work commits or rolls back atomically.
    If the block raises or ``commit`` is never called, every mutation, including
    idempotency, audit, and outbox rows, is rolled back so the last committed
    canonical state is preserved.
    """

    connection: Connection
    idempotency: SqlIdempotencyStore
    audit: SqlAuditLog
    outbox: SqlOutboxStore

    def __init__(self, engine: Engine, clock: Clock = system_clock) -> None:
        self._engine = engine
        self._clock = clock
        self._committed = False

    def __enter__(self) -> "SqlUnitOfWork":
        self.connection = self._engine.connect()
        self._transaction = self.connection.begin()
        self._committed = False
        self.idempotency = SqlIdempotencyStore(self.connection, self._clock)
        self.audit = SqlAuditLog(self.connection, self._clock)
        self.outbox = SqlOutboxStore(self.connection, self._clock)
        return self

    def commit(self) -> None:
        self._transaction.commit()
        self._committed = True

    def rollback(self) -> None:
        if self._transaction.is_active:
            self._transaction.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None or not self._committed:
                self.rollback()
        finally:
            self.connection.close()


def unit_of_work(engine: Engine, clock: Clock = system_clock) -> SqlUnitOfWork:
    """Create a new transactional unit of work bound to ``engine``."""
    return SqlUnitOfWork(engine, clock)
