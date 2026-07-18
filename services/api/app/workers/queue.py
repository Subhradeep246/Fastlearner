from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class JobNotification:
    """A lightweight, ID-based notification published to the job queue.

    The notification carries no learner content; the transactional outbox row
    remains the canonical source of the job payload.
    """

    job_id: UUID
    owner_user_id: UUID
    kind: str


class QueuePublisher(Protocol):
    """Port for the Redis-compatible queue that wakes workers.

    Publishing is best-effort notification only. Losing the queue cannot erase
    committed intent because workers also poll the durable outbox.
    """

    def publish(self, notification: JobNotification) -> None: ...


class NoopQueue:
    """Queue publisher used for local degraded operation and DB-polling workers."""

    def publish(self, notification: JobNotification) -> None:  # noqa: D401 - port impl
        return None


class InMemoryQueue:
    """In-memory queue used by tests and single-process development."""

    def __init__(self) -> None:
        self.published: list[JobNotification] = []

    def publish(self, notification: JobNotification) -> None:
        self.published.append(notification)
