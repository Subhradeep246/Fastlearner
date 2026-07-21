"""Common API middleware.

The request-id middleware assigns a stable identifier to every request. An
inbound ``X-Request-ID`` is honored when present so identifiers can correlate
across clients and services; otherwise a fresh identifier is generated. The
identifier is exposed on ``request.state`` for handlers and error envelopes and
echoed on every response header (Requirement 17.14).
"""

from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_INBOUND_LENGTH = 128


def _resolve_request_id(request: Request) -> str:
    inbound = request.headers.get(REQUEST_ID_HEADER, "").strip()
    if inbound and len(inbound) <= _MAX_INBOUND_LENGTH and inbound.isprintable():
        return inbound
    return str(uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request identifier to the request state and every response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _resolve_request_id(request)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
