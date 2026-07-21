"""Request-scoped dependencies: request IDs, authentication, and idempotency.

The authentication dependency resolves an :class:`ActorContext` from the inbound
bearer token using the configured identity provider. Owner scope is always
derived server-side (Requirement 17.11). The idempotency helper enforces and
applies an ``Idempotency-Key`` on every write (Requirements 17.8, 17.9, 17.10).
"""

from __future__ import annotations

from typing import Callable, TypeVar, cast
from uuid import UUID

from fastapi import Request
from sqlalchemy import Engine

from app.api.errors import REQUEST_ID_HEADER, ApiConfigurationError
from app.auth.identity import IdentityProvider
from app.auth.policy import PolicyEngine
from app.clock import Clock, system_clock
from app.domain.identity import ActorContext, AuthenticationError, NotFoundError
from app.repositories.identity import SqlIdentityRepository
from app.repositories.idempotency import hash_request
from app.repositories.unit_of_work import unit_of_work
from app.services.identity import IdentityService

T = TypeVar("T")


def get_request_id(request: Request) -> str:
    """Return the request identifier assigned by the request-id middleware."""
    request_id = getattr(request.state, "request_id", None)
    if request_id is None:  # pragma: no cover - middleware always sets this
        raise ApiConfigurationError("Request identifier middleware is not installed.")
    return cast(str, request_id)


def _engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise ApiConfigurationError("The API is not configured with a database engine.")
    return cast(Engine, engine)


def _identity_provider(request: Request) -> IdentityProvider:
    provider = getattr(request.app.state, "identity_provider", None)
    if provider is None:
        raise ApiConfigurationError("The API is not configured with an identity provider.")
    return cast(IdentityProvider, provider)


def _policy_engine(request: Request) -> PolicyEngine:
    policy = getattr(request.app.state, "policy_engine", None)
    return policy if isinstance(policy, PolicyEngine) else PolicyEngine()


def _clock(request: Request) -> Clock:
    clock = getattr(request.app.state, "clock", None)
    return clock if callable(clock) else system_clock


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("A bearer token is required.")
    return token.strip()


def get_actor(request: Request) -> ActorContext:
    """Authenticate the caller and resolve the effective owner scope.

    A fresh read-only connection is used to verify the session and re-resolve
    the actor context from live relationship state, so revocation ends access on
    the next request.
    """
    token = _bearer_token(request)
    provider = _identity_provider(request)
    engine = _engine(request)
    with engine.connect() as connection:
        repository = SqlIdentityRepository(connection)
        return provider.authenticate(repository, token)


def get_idempotency_key(request: Request) -> str:
    """Return the ``Idempotency-Key`` header or raise a typed validation error."""
    from app.repositories.errors import MissingIdempotencyKey

    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise MissingIdempotencyKey("write")
    return key


class IdentityReader:
    """Runs a read against a short-lived, owner-scoped identity service.

    Each ``run`` opens a fresh read connection, constructs an
    :class:`IdentityService`, and returns whatever the supplied reader produces.
    """

    def __init__(self, engine: Engine, policy: PolicyEngine) -> None:
        self._engine = engine
        self._policy = policy

    def run(self, reader: Callable[[IdentityService], T]) -> T:
        with self._engine.connect() as connection:
            service = IdentityService(SqlIdentityRepository(connection), self._policy)
            return reader(service)


def read_identity_service(request: Request) -> IdentityReader:
    """Provide a scoped :class:`IdentityReader` for read-only endpoints."""
    return IdentityReader(_engine(request), _policy_engine(request))


def perform_identity_write(
    request: Request,
    actor: ActorContext,
    *,
    operation: str,
    key: str,
    payload: object,
    mutate: Callable[[IdentityService], T],
    reload: Callable[[IdentityService, UUID], T | None],
    result_ref: Callable[[T], UUID],
) -> T:
    """Run an idempotent, transactional identity mutation.

    The operation-scoped idempotency key ``(owner, operation, key)`` is claimed
    before the mutation. A repeated completed key replays the original outcome
    without repeating the mutation (Requirement 17.10). Any failure, including an
    authorization denial inside ``mutate``, rolls back the claim and the
    mutation together so canonical state is unchanged (Requirement 17.15).
    """
    engine = _engine(request)
    policy = _policy_engine(request)
    clock = _clock(request)
    request_hash = hash_request(payload)

    with unit_of_work(engine, clock) as uow:
        service = IdentityService(SqlIdentityRepository(uow.connection), policy)
        claim = uow.idempotency.begin(
            owner_user_id=actor.owner_id,
            operation=operation,
            key=key,
            request_hash=request_hash,
        )
        if claim.completed:
            outcome = claim.outcome
            assert outcome is not None and outcome.result_ref is not None
            replayed = reload(service, outcome.result_ref)
            if replayed is None:
                raise NotFoundError()
            uow.commit()
            return replayed

        resource = mutate(service)
        uow.idempotency.complete(
            owner_user_id=actor.owner_id,
            operation=operation,
            key=key,
            response_status=200,
            result_ref=result_ref(resource),
        )
        uow.commit()
        return resource


__all__ = [
    "ApiConfigurationError",
    "REQUEST_ID_HEADER",
    "get_actor",
    "get_idempotency_key",
    "get_request_id",
    "IdentityReader",
    "perform_identity_write",
    "read_identity_service",
]
