from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

# A jitter function maps a computed backoff ceiling to an actual delay in the
# closed interval ``[0, ceiling]``. The default applies full jitter; tests inject
# deterministic variants.
Jitter = Callable[[float], float]


def full_jitter(ceiling: float) -> float:
    return random.uniform(0.0, ceiling)


class JobKind(StrEnum):
    """Durable job kinds processed through the transactional outbox."""

    SOURCE_CHUNKING = "source_chunking"
    EMBEDDING = "embedding"
    GRAPH_INGESTION = "graph_ingestion"
    GRAPH_RETRACTION = "graph_retraction"
    PHYSICAL_CLEANUP = "physical_cleanup"
    EXPORT_ASSEMBLY = "export_assembly"
    MALWARE_SCAN = "malware_scan"
    REMINDER = "reminder"
    AGGREGATE_REFRESH = "aggregate_refresh"


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter and a dead-letter threshold."""

    max_attempts: int
    base_seconds: float
    max_seconds: float

    def should_retry(self, attempt: int) -> bool:
        """Return whether another attempt is allowed after ``attempt`` failures."""
        return attempt < self.max_attempts

    def delay_seconds(self, attempt: int, jitter: Jitter = full_jitter) -> float:
        """Compute the backoff delay before the next attempt.

        ``attempt`` is the number of attempts already made (>= 1). The ceiling
        grows exponentially and is capped by ``max_seconds``.
        """
        exponent = max(0, attempt - 1)
        ceiling = min(self.max_seconds, self.base_seconds * float(2**exponent))
        return jitter(ceiling)


# Per-kind policies. Deletion and retraction retry aggressively because failure
# must never silently restore retrieval eligibility; reminders retry modestly.
DEFAULT_POLICY = RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=300.0)

POLICIES: dict[str, RetryPolicy] = {
    JobKind.SOURCE_CHUNKING.value: RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=120.0),
    JobKind.EMBEDDING.value: RetryPolicy(max_attempts=6, base_seconds=2.0, max_seconds=300.0),
    JobKind.GRAPH_INGESTION.value: RetryPolicy(max_attempts=8, base_seconds=5.0, max_seconds=900.0),
    JobKind.GRAPH_RETRACTION.value: RetryPolicy(max_attempts=10, base_seconds=5.0, max_seconds=900.0),
    JobKind.PHYSICAL_CLEANUP.value: RetryPolicy(max_attempts=10, base_seconds=5.0, max_seconds=900.0),
    JobKind.EXPORT_ASSEMBLY.value: RetryPolicy(max_attempts=4, base_seconds=3.0, max_seconds=300.0),
    JobKind.MALWARE_SCAN.value: RetryPolicy(max_attempts=4, base_seconds=2.0, max_seconds=120.0),
    JobKind.REMINDER.value: RetryPolicy(max_attempts=3, base_seconds=10.0, max_seconds=600.0),
    JobKind.AGGREGATE_REFRESH.value: RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=180.0),
}


def policy_for(kind: str) -> RetryPolicy:
    """Return the retry policy for a job kind, falling back to the default."""
    return POLICIES.get(kind, DEFAULT_POLICY)
