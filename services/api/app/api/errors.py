"""Typed error envelopes and exception handlers for the versioned API.

Every error response carries a typed error code, a safe non-disclosing message,
the request identifier, and applicable field/issue details (Requirement 17.14).
Authentication and authorization failures never leak protected content, and a
not-found within the authorized scope is indistinguishable from a foreign-owner
record (Requirements 17.12, 17.15).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import StartupConfigurationError
from app.domain.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    NotFoundError,
    ValidationError as IdentityValidationError,
)
from app.persistence.checks import DatabaseMigrationRequired
from app.repositories.errors import (
    IdempotencyInProgress,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
    WorkflowError,
)

REQUEST_ID_HEADER = "X-Request-ID"


class ApiConfigurationError(RuntimeError):
    """Raised when a request needs a dependency the app was not composed with."""

    code = "api_not_configured"
    retryable = False

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def _status_for(exc: Exception) -> int:
    """Map a typed domain/workflow error to an HTTP status code."""
    if isinstance(exc, AuthenticationError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, AuthorizationError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, NotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, (IdentityValidationError, MissingIdempotencyKey)):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(exc, (IdempotencyKeyConflict, IdempotencyInProgress)):
        return status.HTTP_409_CONFLICT
    if isinstance(
        exc, (StartupConfigurationError, DatabaseMigrationRequired, ApiConfigurationError)
    ):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_400_BAD_REQUEST


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_envelope(payload: dict[str, Any], *, request_id: str | None) -> dict[str, Any]:
    """Wrap a safe error payload in the standard typed envelope."""
    body = dict(payload)
    body.setdefault("code", "error")
    body.setdefault("message", "The request could not be completed.")
    body.setdefault("retryable", False)
    body["request_id"] = request_id
    return {"error": body}


def _json_error(
    request: Request, status_code: int, payload: dict[str, Any]
) -> JSONResponse:
    request_id = request_id_of(request)
    response = JSONResponse(
        status_code=status_code,
        content=error_envelope(payload, request_id=request_id),
    )
    if request_id is not None:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def register_exception_handlers(app: FastAPI) -> None:
    """Install typed error-envelope handlers for domain and framework errors."""

    @app.exception_handler(IdentityError)
    async def _identity_error(request: Request, exc: IdentityError) -> JSONResponse:
        return _json_error(request, _status_for(exc), exc.safe_payload())

    @app.exception_handler(WorkflowError)
    async def _workflow_error(request: Request, exc: WorkflowError) -> JSONResponse:
        return _json_error(request, _status_for(exc), exc.safe_payload())

    @app.exception_handler(StartupConfigurationError)
    async def _startup_error(
        request: Request, exc: StartupConfigurationError
    ) -> JSONResponse:
        return _json_error(request, _status_for(exc), exc.safe_payload())

    @app.exception_handler(DatabaseMigrationRequired)
    async def _migration_error(
        request: Request, exc: DatabaseMigrationRequired
    ) -> JSONResponse:
        return _json_error(request, _status_for(exc), exc.safe_payload())

    @app.exception_handler(ApiConfigurationError)
    async def _api_configuration_error(
        request: Request, exc: ApiConfigurationError
    ) -> JSONResponse:
        return _json_error(request, _status_for(exc), exc.safe_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc", ())
        field = str(location[-1]) if location else None
        payload: dict[str, Any] = {
            "code": "validation_error",
            "message": "The request payload failed validation.",
            "retryable": False,
        }
        if field is not None:
            payload["field"] = field
        return _json_error(request, status.HTTP_422_UNPROCESSABLE_ENTITY, payload)
