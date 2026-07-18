"""Secure session token contracts.

Development and future OIDC sessions are represented as short-lived, signed,
opaque bearer tokens. The signing secret stays server-side; the database stores
only a one-way hash of the issued token so a leaked row cannot reconstruct a
usable credential. Verification is constant-time and binds each token to a
session record, actor, owner scope, and session version so revocation and
version bumps end access immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.domain.identity import AuthenticationError

#: Default lifetime for issued sessions. Sessions are deliberately short-lived.
DEFAULT_SESSION_TTL = timedelta(hours=12)

_TOKEN_VERSION = "v1"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True)
class SessionClaims:
    """The signed, verifiable contents of a session token."""

    session_id: UUID
    actor_user_id: UUID
    owner_user_id: UUID
    session_version: int
    expires_at: datetime

    def _payload(self) -> dict[str, object]:
        return {
            "sid": str(self.session_id),
            "act": str(self.actor_user_id),
            "own": str(self.owner_user_id),
            "ver": self.session_version,
            "exp": int(self.expires_at.timestamp()),
        }

    @classmethod
    def _from_payload(cls, payload: dict[str, object]) -> "SessionClaims":
        try:
            return cls(
                session_id=UUID(str(payload["sid"])),
                actor_user_id=UUID(str(payload["act"])),
                owner_user_id=UUID(str(payload["own"])),
                session_version=int(str(payload["ver"])),
                expires_at=datetime.fromtimestamp(int(str(payload["exp"])), tz=timezone.utc),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise AuthenticationError("Session token is malformed.") from error


@dataclass(frozen=True)
class IssuedSession:
    """A freshly issued token plus the material persisted for verification."""

    token: str
    claims: SessionClaims
    token_hash: bytes


def token_hash(token: str) -> bytes:
    """One-way hash stored in the ``sessions`` table for lookup/verification."""
    return hashlib.sha256(token.encode("utf-8")).digest()


class SessionSigner:
    """Signs and verifies opaque session bearer tokens using HMAC-SHA256."""

    def __init__(self, signing_secret: str) -> None:
        secret = (signing_secret or "").strip()
        if not secret:
            raise ValueError("A non-empty session signing secret is required")
        self._key = secret.encode("utf-8")

    def _sign(self, message: bytes) -> bytes:
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def issue(
        self,
        *,
        session_id: UUID,
        actor_user_id: UUID,
        owner_user_id: UUID,
        session_version: int = 1,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        issued_at: datetime | None = None,
    ) -> IssuedSession:
        now = issued_at or datetime.now(timezone.utc)
        claims = SessionClaims(
            session_id=session_id,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            session_version=session_version,
            expires_at=now + ttl,
        )
        body = json.dumps(claims._payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded_body = _b64encode(body)
        signature = _b64encode(self._sign(f"{_TOKEN_VERSION}.{encoded_body}".encode("ascii")))
        token = f"{_TOKEN_VERSION}.{encoded_body}.{signature}"
        return IssuedSession(token=token, claims=claims, token_hash=token_hash(token))

    def verify(self, token: str, *, at: datetime | None = None) -> SessionClaims:
        """Verify signature and expiry, returning the claims.

        Raises :class:`AuthenticationError` for any malformed, tampered, or
        expired token. This checks the cryptographic envelope only; the caller
        must still confirm the session record is active and version-matched.
        """
        if not token:
            raise AuthenticationError("Authentication is required.")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
            raise AuthenticationError("Session token is malformed.")
        _, encoded_body, provided_signature = parts
        expected = self._sign(f"{_TOKEN_VERSION}.{encoded_body}".encode("ascii"))
        try:
            provided = _b64decode(provided_signature)
        except (ValueError, TypeError) as error:
            raise AuthenticationError("Session token is malformed.") from error
        if not hmac.compare_digest(expected, provided):
            raise AuthenticationError("Session token signature is invalid.")
        try:
            payload = json.loads(_b64decode(encoded_body))
        except (ValueError, TypeError) as error:
            raise AuthenticationError("Session token is malformed.") from error
        claims = SessionClaims._from_payload(payload)
        now = at or datetime.now(timezone.utc)
        if claims.expires_at <= now:
            raise AuthenticationError("Session has expired.")
        return claims
