"""Durable background worker primitives for the transactional outbox.

Provides retry/backoff and dead-letter policy, a best-effort queue relay, and a
lease-based durable worker that recovers work after worker failure.
"""

from app.workers.policy import (
    DEFAULT_POLICY,
    POLICIES,
    JobKind,
    RetryPolicy,
    full_jitter,
    policy_for,
)
from app.workers.queue import InMemoryQueue, JobNotification, NoopQueue, QueuePublisher
from app.workers.relay import OutboxRelay
from app.workers.worker import DurableWorker, Handler, JobContext, ProcessResult

__all__ = [
    "DEFAULT_POLICY",
    "POLICIES",
    "DurableWorker",
    "Handler",
    "InMemoryQueue",
    "JobContext",
    "JobKind",
    "JobNotification",
    "NoopQueue",
    "OutboxRelay",
    "ProcessResult",
    "QueuePublisher",
    "RetryPolicy",
    "full_jitter",
    "policy_for",
]
