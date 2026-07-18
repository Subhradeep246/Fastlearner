from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine, select

from app.clock import Clock, system_clock
from app.persistence.models import JobStatus, outbox_jobs
from app.workers.queue import JobNotification, QueuePublisher


class OutboxRelay:
    """Publishes committed outbox intent to the queue as ID-based notifications.

    The relay never mutates the outbox; it only notifies workers that ready work
    exists. Because the outbox is canonical, dropped notifications only delay
    processing until the next poll, they never lose committed intent.
    """

    def __init__(self, engine: Engine, publisher: QueuePublisher, clock: Clock = system_clock) -> None:
        self._engine = engine
        self._publisher = publisher
        self._clock = clock

    def publish_ready(self, *, limit: int = 100, now: datetime | None = None) -> int:
        """Publish notifications for ready pending/retry jobs. Returns the count."""
        moment = now if now is not None else self._clock()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    outbox_jobs.c.id,
                    outbox_jobs.c.owner_user_id,
                    outbox_jobs.c.kind,
                )
                .where(
                    outbox_jobs.c.status.in_(
                        (JobStatus.PENDING.value, JobStatus.RETRY_WAIT.value)
                    ),
                    outbox_jobs.c.available_at <= moment,
                )
                .order_by(outbox_jobs.c.available_at)
                .limit(limit)
            ).all()

        for row in rows:
            self._publisher.publish(
                JobNotification(job_id=row.id, owner_user_id=row.owner_user_id, kind=row.kind)
            )
        return len(rows)
