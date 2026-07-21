"""Centralized authorization policy enforcement.

``PolicyEngine.authorize`` is the single decision point every application
service consults *before* it looks up a body-driven resource or performs a
mutation. It encodes the relationship- and resource-based rules from the design:

1. Learner ownership grants the enabled learner actions on the learner's own
   data (Requirement 2.5, 2.6).
2. An observer relationship grants only the listed ``*:read`` scopes, and only
   while the relationship is active; the effective scope is the intersection of
   the endpoint-required scope with the relationship ``permission_scope``
   (Requirement 2.5, 2.7).
3. Observer sessions are read-only; any learner-data mutation is denied
   (Requirement 2.6).
4. A denied request is recorded pseudonymously (actor pseudonym, requested
   resource kind, scope decision, request identifier) and never returns
   protected data or cross-owner existence (Requirements 19.10, 21.5).

Revocation ends access immediately because the effective ``ActorContext`` is
re-resolved from live relationship state on every request; a revoked or expired
relationship yields no scopes, so every scoped read is denied here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.identity import ActorContext, AuthorizationError, Scope

_logger = logging.getLogger("app.authorization")


class AccessMode(StrEnum):
    """The kind of access a caller is attempting on a resource."""

    READ = "read"
    WRITE = "write"


class ResourceKind(StrEnum):
    """Resource families the policy engine reasons about.

    Kinds that map to an observer-grantable ``*:read`` scope may be read by an
    observer holding that scope. Owner-only kinds (profile, device,
    relationship, subject) are never observable and are reserved for the learner
    owner.
    """

    DASHBOARD = "dashboard"
    ASSIGNMENTS = "assignments"
    LEARNING = "learning"
    MEMORY = "memory"
    PATHWAYS = "pathways"
    PROFILE = "profile"
    DEVICE = "device"
    RELATIONSHIP = "relationship"


#: Maps a resource kind to the read scope an observer relationship must hold to
#: read it. Kinds absent from this map are owner-only and never observable.
_OBSERVER_READ_SCOPE: dict[ResourceKind, Scope] = {
    ResourceKind.DASHBOARD: Scope.DASHBOARD_READ,
    ResourceKind.PROFILE: Scope.DASHBOARD_READ,
    ResourceKind.ASSIGNMENTS: Scope.ASSIGNMENTS_READ,
    ResourceKind.LEARNING: Scope.LEARNING_READ,
    ResourceKind.MEMORY: Scope.MEMORY_READ,
    ResourceKind.PATHWAYS: Scope.PATHWAYS_READ,
}


def pseudonymize(actor_id: UUID) -> str:
    """Derive a stable, non-reversible pseudonym for an actor.

    Denial records must identify an actor for correlation without persisting a
    directly usable identity (Requirement 21.5). A SHA-256 digest over the
    canonical UUID bytes is deterministic for correlation yet does not expose
    the raw identifier.
    """
    return hashlib.sha256(actor_id.bytes).hexdigest()[:16]


@dataclass(frozen=True)
class PolicyDenial:
    """A pseudonymous, content-free record of an authorization denial."""

    actor_pseudonym: str
    resource_kind: str
    action: str
    decision: str
    reason_code: str
    request_id: str | None = None


class DenialSink(Protocol):
    """Port for recording authorization denials.

    Implementations MUST NOT persist protected resource data or raw learner
    identifiers; only the pseudonymous, content-free ``PolicyDenial`` is passed.
    """

    def record_denial(self, denial: PolicyDenial) -> None: ...


class NullDenialSink(DenialSink):
    """A denial sink that discards records; used where auditing is not wired."""

    def record_denial(self, denial: PolicyDenial) -> None:  # noqa: D401 - no-op
        return None


class LoggingDenialSink(DenialSink):
    """Default denial sink that emits a structured, pseudonymous log line."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or _logger

    def record_denial(self, denial: PolicyDenial) -> None:
        self._logger.info(
            "authorization_denied",
            extra={
                "actor_pseudonym": denial.actor_pseudonym,
                "resource_kind": denial.resource_kind,
                "action": denial.action,
                "decision": denial.decision,
                "reason_code": denial.reason_code,
                "request_id": denial.request_id,
            },
        )


class PolicyEngine:
    """The single authorization decision point for application services."""

    def __init__(self, denial_sink: DenialSink | None = None) -> None:
        self._denials = denial_sink or LoggingDenialSink()

    def authorize(
        self,
        actor: ActorContext,
        action: AccessMode,
        resource_kind: ResourceKind,
        *,
        subject_id: UUID | None = None,  # noqa: ARG002 - reserved for subject-scoped checks
        request_id: str | UUID | None = None,
    ) -> None:
        """Authorize ``actor`` for ``action`` on ``resource_kind`` or raise.

        On denial a pseudonymous record is written and an
        :class:`AuthorizationError` is raised with a safe, non-disclosing
        message. This must be called before any body-driven resource lookup or
        service mutation.
        """
        # The learner owner holds every enabled action on their own data.
        if actor.is_owner:
            return

        # Any non-owner performing a learner-data mutation is denied; observer
        # sessions are strictly read-only (Requirement 2.6).
        if action is AccessMode.WRITE:
            self._deny(actor, action, resource_kind, "observer_read_only", request_id)

        # Non-owner, non-observer actors have no standing at all.
        if not actor.is_observer:
            self._deny(actor, action, resource_kind, "not_authorized", request_id)

        # Observer reads require the mapped, actively granted read scope. A
        # revoked/expired relationship resolves to no scopes and is denied here.
        required = _OBSERVER_READ_SCOPE.get(resource_kind)
        if required is None:
            self._deny(actor, action, resource_kind, "resource_not_observable", request_id)
        elif not actor.has_scope(required.value):
            self._deny(actor, action, resource_kind, "scope_not_granted", request_id)

    def _deny(
        self,
        actor: ActorContext,
        action: AccessMode,
        resource_kind: ResourceKind,
        reason_code: str,
        request_id: str | UUID | None,
    ) -> None:
        self._denials.record_denial(
            PolicyDenial(
                actor_pseudonym=pseudonymize(actor.actor_id),
                resource_kind=str(resource_kind),
                action=str(action),
                decision="denied",
                reason_code=reason_code,
                request_id=str(request_id) if request_id is not None else None,
            )
        )
        raise AuthorizationError()
