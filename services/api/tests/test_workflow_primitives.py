from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.persistence.models import (
    JobStatus,
    audit_records,
    idempotency_records,
    job_runs,
    metadata,
    outbox_jobs,
)
from app.repositories import (
    IdempotencyInProgress,
    IdempotencyKeyConflict,
    SqlUnitOfWork,
    hash_request,
    unit_of_work,
)
from app.repositories.errors import UnknownJobKind
from app.workers.policy import RetryPolicy
from app.workers.queue import InMemoryQueue
from app.workers.relay import OutboxRelay
from app.workers.worker import DurableWorker, JobContext

OWNER = UUID("00000000-0000-4000-8000-000000000001")
ACTOR = UUID("00000000-0000-4000-8000-000000000009")


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> "FakeClock":
        self.now = self.now + timedelta(seconds=seconds)
        return self


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _clock() -> FakeClock:
    return FakeClock(datetime(2025, 1, 1, tzinfo=timezone.utc))


# -- idempotency ---------------------------------------------------------


def test_idempotency_first_claim_is_new_and_replays_completed_outcome() -> None:
    engine = _engine()
    clock = _clock()
    request_hash = hash_request({"title": "Fractions", "due": "2025-02-01"})
    result_ref = uuid4()

    with unit_of_work(engine, clock) as uow:
        claim = uow.idempotency.begin(
            owner_user_id=OWNER, operation="assignments.create", key="key-1", request_hash=request_hash
        )
        assert claim.is_new is True
        uow.idempotency.complete(
            owner_user_id=OWNER,
            operation="assignments.create",
            key="key-1",
            response_status=201,
            result_ref=result_ref,
        )
        uow.commit()

    with unit_of_work(engine, clock) as uow:
        replay = uow.idempotency.begin(
            owner_user_id=OWNER, operation="assignments.create", key="key-1", request_hash=request_hash
        )
        assert replay.is_new is False
        assert replay.completed is True
        assert replay.outcome is not None
        assert replay.outcome.response_status == 201
        assert replay.outcome.result_ref == result_ref


def test_idempotency_same_key_different_request_is_typed_conflict() -> None:
    engine = _engine()
    clock = _clock()
    with unit_of_work(engine, clock) as uow:
        uow.idempotency.begin(
            owner_user_id=OWNER,
            operation="assignments.create",
            key="key-1",
            request_hash=hash_request({"title": "A"}),
        )
        uow.idempotency.complete(
            owner_user_id=OWNER, operation="assignments.create", key="key-1", response_status=201
        )
        uow.commit()

    with unit_of_work(engine, clock) as uow:
        with pytest.raises(IdempotencyKeyConflict) as caught:
            uow.idempotency.begin(
                owner_user_id=OWNER,
                operation="assignments.create",
                key="key-1",
                request_hash=hash_request({"title": "B"}),
            )
    assert caught.value.code == "idempotency_key_conflict"


def test_idempotency_in_progress_when_prior_claim_not_completed() -> None:
    engine = _engine()
    clock = _clock()
    request_hash = hash_request({"title": "A"})
    # Simulate a committed pending claim (e.g. a crashed prior attempt that
    # committed the claim but not the completion).
    with engine.begin() as connection:
        connection.execute(
            idempotency_records.insert().values(
                id=uuid4(),
                owner_user_id=OWNER,
                operation="assignments.create",
                key="key-1",
                request_hash=request_hash,
                response_status=None,
                response_body_hash=None,
                result_ref=None,
                expires_at=None,
            )
        )

    with unit_of_work(engine, clock) as uow:
        with pytest.raises(IdempotencyInProgress):
            uow.idempotency.begin(
                owner_user_id=OWNER,
                operation="assignments.create",
                key="key-1",
                request_hash=request_hash,
            )


# -- unit of work rollback ----------------------------------------------


def test_unit_of_work_rolls_back_all_writes_on_error() -> None:
    engine = _engine()
    clock = _clock()
    with pytest.raises(RuntimeError, match="boom"):
        with unit_of_work(engine, clock) as uow:
            uow.audit.record(
                owner_user_id=OWNER,
                actor_user_id=ACTOR,
                action="assignment.create",
                resource_kind="assignment",
                resource_id=uuid4(),
                request_id="req-1",
            )
            uow.outbox.enqueue(
                owner_user_id=OWNER,
                kind="reminder",
                deduplication_key="dedup-1",
                payload={"assignment_id": str(uuid4())},
            )
            raise RuntimeError("boom")

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(audit_records)) == 0
        assert connection.scalar(select(func.count()).select_from(outbox_jobs)) == 0


def test_unit_of_work_commit_persists_audit_and_outbox() -> None:
    engine = _engine()
    clock = _clock()
    with unit_of_work(engine, clock) as uow:
        uow.audit.record(
            owner_user_id=OWNER,
            actor_user_id=ACTOR,
            action="assignment.create",
            resource_kind="assignment",
            resource_id=uuid4(),
            request_id="req-1",
        )
        uow.commit()

    with engine.connect() as connection:
        row = connection.execute(select(audit_records)).mappings().one()
        assert row["action"] == "assignment.create"
        assert row["details"]["request_id"] == "req-1"


# -- outbox --------------------------------------------------------------


def test_outbox_enqueue_deduplicates_committed_intent() -> None:
    engine = _engine()
    clock = _clock()
    payload = {"episode_id": str(uuid4())}
    with unit_of_work(engine, clock) as uow:
        first = uow.outbox.enqueue(
            owner_user_id=OWNER, kind="graph_ingestion", deduplication_key="ep-1", payload=payload
        )
        second = uow.outbox.enqueue(
            owner_user_id=OWNER, kind="graph_ingestion", deduplication_key="ep-1", payload=payload
        )
        uow.commit()

    assert first.created is True
    assert second.created is False
    assert first.job_id == second.job_id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(outbox_jobs)) == 1


# -- durable worker ------------------------------------------------------


def _enqueue(engine, clock, kind: str, key: str) -> UUID:
    with unit_of_work(engine, clock) as uow:
        result = uow.outbox.enqueue(
            owner_user_id=OWNER,
            kind=kind,
            deduplication_key=key,
            payload={"target_id": str(uuid4())},
        )
        uow.commit()
    return result.job_id


def test_worker_processes_job_and_marks_succeeded() -> None:
    engine = _engine()
    clock = _clock()
    job_id = _enqueue(engine, clock, "reminder", "r-1")
    seen: list[JobContext] = []
    worker = DurableWorker(
        engine, handlers={"reminder": seen.append}, clock=clock, worker_id="w-test"
    )

    result = worker.process_next()
    assert result is not None
    assert result.status is JobStatus.SUCCEEDED
    assert len(seen) == 1
    assert seen[0].job_id == job_id
    assert seen[0].attempt == 1

    with engine.connect() as connection:
        job = connection.execute(select(outbox_jobs).where(outbox_jobs.c.id == job_id)).mappings().one()
        assert job["status"] == JobStatus.SUCCEEDED.value
        assert job["leased_until"] is None
        run = connection.execute(select(job_runs).where(job_runs.c.outbox_job_id == job_id)).mappings().one()
        assert run["status"] == "succeeded"
        assert run["worker_id"] == "w-test"

    # No more work remains.
    assert worker.process_next() is None


def test_worker_retries_with_backoff_then_dead_letters() -> None:
    engine = _engine()
    clock = _clock()
    job_id = _enqueue(engine, clock, "reminder", "r-1")

    def always_fails(_context: JobContext) -> None:
        raise RuntimeError("handler failed")

    worker = DurableWorker(
        engine,
        handlers={"reminder": always_fails},
        clock=clock,
        policy_resolver=lambda _kind: RetryPolicy(max_attempts=2, base_seconds=1.0, max_seconds=1.0),
        jitter=lambda _ceiling: 0.0,
    )

    first = worker.process_next()
    assert first is not None
    assert first.status is JobStatus.RETRY_WAIT
    with engine.connect() as connection:
        job = connection.execute(select(outbox_jobs).where(outbox_jobs.c.id == job_id)).mappings().one()
        assert job["status"] == JobStatus.RETRY_WAIT.value
        assert job["attempt_count"] == 1
        assert job["last_error_code"] == "RuntimeError"

    second = worker.process_next()
    assert second is not None
    assert second.status is JobStatus.DEAD_LETTER
    with engine.connect() as connection:
        job = connection.execute(select(outbox_jobs).where(outbox_jobs.c.id == job_id)).mappings().one()
        assert job["status"] == JobStatus.DEAD_LETTER.value
        assert job["attempt_count"] == 2
        runs = connection.execute(
            select(job_runs.c.status).where(job_runs.c.outbox_job_id == job_id).order_by(job_runs.c.attempt)
        ).scalars().all()
        assert runs == ["retry_wait", "dead_letter"]


def test_worker_recovers_expired_lease_after_worker_failure() -> None:
    engine = _engine()
    clock = _clock()
    _enqueue(engine, clock, "reminder", "r-1")

    # First worker claims the job but "dies" before completing it.
    dead_worker = DurableWorker(engine, handlers={}, clock=clock, lease_seconds=60.0, worker_id="dead")
    claimed = dead_worker.claim()
    assert claimed is not None
    assert claimed.attempt == 1

    # Before lease expiry the job is not eligible.
    assert dead_worker.claim() is None

    # After the lease expires, a recovering worker re-claims the job.
    clock.advance(120)
    recovered = dead_worker.claim()
    assert recovered is not None
    assert recovered.attempt == 2


def test_worker_dead_letters_unknown_job_kind() -> None:
    engine = _engine()
    clock = _clock()
    _enqueue(engine, clock, "reminder", "r-1")
    worker = DurableWorker(
        engine,
        handlers={},
        clock=clock,
        policy_resolver=lambda _kind: RetryPolicy(max_attempts=1, base_seconds=1.0, max_seconds=1.0),
    )
    result = worker.process_next()
    assert result is not None
    assert result.status is JobStatus.DEAD_LETTER
    assert result.error_code == UnknownJobKind("reminder").code


# -- relay ---------------------------------------------------------------


def test_relay_publishes_ready_jobs_without_mutating_outbox() -> None:
    engine = _engine()
    clock = _clock()
    job_id = _enqueue(engine, clock, "graph_ingestion", "ep-1")
    queue = InMemoryQueue()
    relay = OutboxRelay(engine, queue, clock=clock)

    published = relay.publish_ready()
    assert published == 1
    assert len(queue.published) == 1
    assert queue.published[0].job_id == job_id
    assert queue.published[0].kind == "graph_ingestion"

    with engine.connect() as connection:
        job = connection.execute(select(outbox_jobs).where(outbox_jobs.c.id == job_id)).mappings().one()
        assert job["status"] == JobStatus.PENDING.value


def test_main_module_is_importable_for_supervisor() -> None:
    from app.workers import main as worker_main

    assert callable(worker_main.main)
    assert isinstance(SqlUnitOfWork(_engine()), SqlUnitOfWork)
