from __future__ import annotations

from typing import Any


class WorkflowError(RuntimeError):
    """Base class for typed, safe-to-surface workflow primitive errors."""

    code: str = "workflow_error"
    retryable: bool = False

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class MissingIdempotencyKey(WorkflowError):
    """Raised when a write operation is attempted without an idempotency key."""

    code = "idempotency_key_required"
    retryable = False

    def __init__(self, operation: str) -> None:
        super().__init__(f"Operation '{operation}' requires an Idempotency-Key")


class IdempotencyKeyConflict(WorkflowError):
    """Raised when an idempotency key is reused with a different request payload."""

    code = "idempotency_key_conflict"
    retryable = False

    def __init__(self, operation: str, key: str) -> None:
        super().__init__(
            f"Idempotency key '{key}' for operation '{operation}' was reused with a different request"
        )


class IdempotencyInProgress(WorkflowError):
    """Raised when a concurrent operation still holds the idempotency key."""

    code = "idempotency_in_progress"
    retryable = True

    def __init__(self, operation: str, key: str) -> None:
        super().__init__(
            f"Idempotency key '{key}' for operation '{operation}' is being processed concurrently"
        )


class UnknownJobKind(WorkflowError):
    """Raised when a durable worker has no handler registered for a job kind."""

    code = "unknown_job_kind"
    retryable = False

    def __init__(self, kind: str) -> None:
        super().__init__(f"No handler is registered for job kind '{kind}'")
