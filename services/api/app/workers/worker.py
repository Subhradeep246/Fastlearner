from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, and_, or_, select

from app.clock import Clock, system_clock
from app.persistence.models import JobStatus, job_runs, outbox_jobs
from app.repositories.errors import UnknownJobKind
from app.workers.policy import Jitter, RetryPolicy, full_jitter, policy_for


@dataclass(frozen=True)
class JobContext:
    """Immutable context passed to a job handler for a single attempt."""

    job_id: UUID
    owner_user_id: UUID
    kind: str
    payload: dict[str, Any]
    attempt: int


Handler = Callable[[JobContext], None]


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of processing a single claimed job."""

    context: JobContext
    status: JobStatus
    error_code: str | None = None


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = type(error).__name__
    return code[:96]


class DurableWorker:
    """Durable, at-least-once outbox worker with leases, retry, and dead-letter.

    Each job is leased in its own committed transaction before the handler runs,
    so a worker crash mid-handler leaves the lease in place. Once the lease
    expires, the job becomes eligible again and is recovered by any worker. The
    compare-and-set claim prevents two workers from processing the same attempt.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        handlers: Mapping[str, Handler] | None = None,
        worker_id: str | None = None,
        clock: Clock = system_clock,
        lease_seconds: float = 60.0,
        jitter: Jitter = full_jitter,
        policy_resolver: Callable[[str], RetryPolicy] = policy_for,
    ) -> None:
        self._engine = engine
        self._handlers: dict[str, Handler] = dict(handlers or {})
        self._worker_id = worker_id or f"worker-{uuid4().hex[:12]}"
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)
        self._jitter = jitter
        self._policy_resolver = policy_resolver

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def claim(self, now: datetime | None = None) -> JobContext | None:
        """Atomically lease the next eligible job in its own transaction."""
        moment = now if now is not None else self._clock()
        with self._engine.begin() as connection:
            for candidate in self._eligible(connection, moment):
                claimed = self._try_claim(connection, candidate, moment)
                if claimed is not None:
                    return claimed
        return None

    def process_next(self, now: datetime | None = None) -> ProcessResult | None:
        """Claim and process one job, returning its result or ``None`` if idle."""
        context = self.claim(now)
        if context is None:
            return None
        try:
            handler = self._handlers.get(context.kind)
            if handler is None:
                raise UnknownJobKind(context.kind)
            handler(context)
        except Exception as error:  # noqa: BLE001 - failures are recorded, not raised
            return self._fail(context, _error_code(error))
        return self._succeed(context)

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        """Poll and process jobs until ``stop_event`` is set."""
        while not stop_event.is_set():
            try:
                processed = self.process_next()
            except Exception:  # noqa: BLE001 - transient store errors must not kill the loop
                stop_event.wait(poll_interval)
                continue
            if processed is None:
                stop_event.wait(poll_interval)

    # -- internal helpers -------------------------------------------------

    def _eligible(self, connection: Connection, moment: datetime) -> list[Any]:
        statement = (
            select(
                outbox_jobs.c.id,
                outbox_jobs.c.owner_user_id,
                outbox_jobs.c.kind,
                outbox_jobs.c.payload,
                outbox_jobs.c.status,
                outbox_jobs.c.attempt_count,
            )
            .where(
                or_(
                    and_(
                        outbox_jobs.c.status.in_(
                            (JobStatus.PENDING.value, JobStatus.RETRY_WAIT.value)
                        ),
                        outbox_jobs.c.available_at <= moment,
                    ),
                    and_(
                        outbox_jobs.c.status == JobStatus.LEASED.value,
                        outbox_jobs.c.leased_until < moment,
                    ),
                )
            )
            .order_by(outbox_jobs.c.available_at)
            .limit(10)
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        return list(connection.execute(statement).all())

    def _try_claim(self, connection: Connection, candidate: Any, moment: datetime) -> JobContext | None:
        attempt = candidate.attempt_count + 1
        result = connection.execute(
            outbox_jobs.update()
            .where(
                outbox_jobs.c.id == candidate.id,
                outbox_jobs.c.status == candidate.status,
                outbox_jobs.c.attempt_count == candidate.attempt_count,
            )
            .values(
                status=JobStatus.LEASED.value,
                leased_until=moment + self._lease,
                attempt_count=attempt,
            )
        )
        if result.rowcount != 1:
            return None
        connection.execute(
            job_runs.insert().values(
                id=uuid4(),
                owner_user_id=candidate.owner_user_id,
                outbox_job_id=candidate.id,
                worker_id=self._worker_id,
                status="leased",
                attempt=attempt,
                started_at=moment,
                finished_at=None,
                error_code=None,
            )
        )
        return JobContext(
            job_id=candidate.id,
            owner_user_id=candidate.owner_user_id,
            kind=candidate.kind,
            payload=dict(candidate.payload or {}),
            attempt=attempt,
        )

    def _succeed(self, context: JobContext) -> ProcessResult:
        moment = self._clock()
        with self._engine.begin() as connection:
            connection.execute(
                outbox_jobs.update()
                .where(outbox_jobs.c.id == context.job_id)
                .values(status=JobStatus.SUCCEEDED.value, leased_until=None, last_error_code=None)
            )
            self._finish_run(connection, context, "succeeded", None, moment)
        return ProcessResult(context=context, status=JobStatus.SUCCEEDED)

    def _fail(self, context: JobContext, error_code: str) -> ProcessResult:
        policy = self._policy_resolver(context.kind)
        moment = self._clock()
        if policy.should_retry(context.attempt):
            delay = policy.delay_seconds(context.attempt, self._jitter)
            available_at = moment + timedelta(seconds=delay)
            job_status = JobStatus.RETRY_WAIT
            run_status = "retry_wait"
        else:
            available_at = None
            job_status = JobStatus.DEAD_LETTER
            run_status = "dead_letter"

        with self._engine.begin() as connection:
            values: dict[str, Any] = {
                "status": job_status.value,
                "leased_until": None,
                "last_error_code": error_code,
            }
            if available_at is not None:
                values["available_at"] = available_at
            connection.execute(
                outbox_jobs.update().where(outbox_jobs.c.id == context.job_id).values(**values)
            )
            self._finish_run(connection, context, run_status, error_code, moment)
        return ProcessResult(context=context, status=job_status, error_code=error_code)

    @staticmethod
    def _finish_run(
        connection: Connection,
        context: JobContext,
        status: str,
        error_code: str | None,
        moment: datetime,
    ) -> None:
        connection.execute(
            job_runs.update()
            .where(
                job_runs.c.outbox_job_id == context.job_id,
                job_runs.c.attempt == context.attempt,
            )
            .values(status=status, finished_at=moment, error_code=error_code)
        )
