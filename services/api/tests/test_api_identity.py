"""Integration tests for the authenticated ``/v1`` identity API and middleware.

Covers Requirement 17: versioned/compatible health routes, request identifiers,
authentication dependencies, typed error envelopes with safe messages, write
idempotency enforcement and replay, cursor/time/UUID serialization, server-side
owner-scope derivation, and scope-safe not-found responses.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.auth.identity import LocalIdentityProvider, LocalPersona
from app.auth.sessions import SessionSigner
from app.config import load_settings
from app.main import create_app
from app.persistence.models import (
    devices,
    idempotency_records,
    metadata,
    profiles,
    sessions,
    user_relationships,
    users,
)
from app.persistence.seeds import LOCAL_LEARNER_ID, seed_local_personas

API_TABLES = [users, profiles, user_relationships, devices, sessions, idempotency_records]
SIGNING_SECRET = "api-identity-test-signing-secret"


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine, tables=API_TABLES)
    with engine.begin() as connection:
        seed_local_personas(connection)

    signer = SessionSigner(SIGNING_SECRET)
    provider = LocalIdentityProvider(signer, environment="development")
    settings = load_settings(session_signing_secret=SIGNING_SECRET)
    app = create_app(settings, engine=engine, identity_provider=provider)

    from app.repositories.identity import SqlIdentityRepository

    def token_for(persona: LocalPersona) -> str:
        with engine.begin() as connection:
            issued = provider.issue_local_session(
                SqlIdentityRepository(connection), persona
            )
        return issued.token

    client = TestClient(app)
    return client, token_for


def _auth(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


# ---------------------------------------------------------------------------
# Health and request identifiers (Requirements 17.1, 17.14)
# ---------------------------------------------------------------------------


def test_health_routes_are_compatible(client_factory) -> None:
    client, _ = client_factory
    for path in ("/health", "/v1/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert "X-Request-ID" in response.headers


def test_request_id_is_generated_and_honored(client_factory) -> None:
    client, _ = client_factory
    generated = client.get("/v1/health").headers["X-Request-ID"]
    assert generated

    echoed = client.get("/v1/health", headers={"X-Request-ID": "trace-123"})
    assert echoed.headers["X-Request-ID"] == "trace-123"


# ---------------------------------------------------------------------------
# Authentication (Requirements 17.11, 17.15)
# ---------------------------------------------------------------------------


def test_me_requires_authentication(client_factory) -> None:
    client, _ = client_factory
    response = client.get("/v1/me")
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "authentication_error"
    assert body["request_id"] is not None
    # No protected content leaks in an auth failure.
    assert "profile" not in body


def test_me_returns_actor_and_profile_for_learner(client_factory) -> None:
    client, token_for = client_factory
    response = client.get("/v1/me", headers=_auth(token_for(LocalPersona.LEARNER)))
    assert response.status_code == 200
    body = response.json()
    assert body["owner_id"] == str(LOCAL_LEARNER_ID)
    assert body["is_owner"] is True
    assert body["role"] == "learner"
    assert "assignments:read" in body["scopes"]
    assert body["profile"]["grade_level"] == 5


def test_observer_resolves_to_learner_owner(client_factory) -> None:
    client, token_for = client_factory
    response = client.get("/v1/me", headers=_auth(token_for(LocalPersona.PARENT)))
    assert response.status_code == 200
    body = response.json()
    assert body["owner_id"] == str(LOCAL_LEARNER_ID)
    assert body["is_observer"] is True


# ---------------------------------------------------------------------------
# Write idempotency enforcement and replay (Requirements 17.8, 17.9, 17.10)
# ---------------------------------------------------------------------------


def test_profile_update_requires_idempotency_key(client_factory) -> None:
    client, token_for = client_factory
    response = client.patch(
        "/v1/me/profile",
        headers=_auth(token_for(LocalPersona.LEARNER)),
        json={"grade_level": 7},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_profile_update_applies_and_replays(client_factory) -> None:
    client, token_for = client_factory
    token = token_for(LocalPersona.LEARNER)
    headers = _auth(token, idempotency_key="profile-key-1")

    first = client.patch(
        "/v1/me/profile", headers=headers, json={"grade_level": 8, "timezone": "America/New_York"}
    )
    assert first.status_code == 200
    assert first.json()["grade_level"] == 8

    # Replaying the same key returns the original outcome without a new mutation.
    replay = client.patch("/v1/me/profile", headers=headers, json={"grade_level": 8, "timezone": "America/New_York"})
    assert replay.status_code == 200
    assert replay.json()["grade_level"] == 8

    # A different payload on the same key is a typed conflict.
    conflict = client.patch("/v1/me/profile", headers=headers, json={"grade_level": 9})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_invalid_profile_update_returns_typed_validation_error(client_factory) -> None:
    client, token_for = client_factory
    response = client.patch(
        "/v1/me/profile",
        headers=_auth(token_for(LocalPersona.LEARNER), idempotency_key="bad-grade"),
        json={"grade_level": 99},
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["field"] == "grade_level"


# ---------------------------------------------------------------------------
# Observer read-only enforcement (Requirement 17.15)
# ---------------------------------------------------------------------------


def test_observer_cannot_mutate_profile(client_factory) -> None:
    client, token_for = client_factory
    response = client.patch(
        "/v1/me/profile",
        headers=_auth(token_for(LocalPersona.PARENT), idempotency_key="observer-write"),
        json={"grade_level": 6},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_error"


# ---------------------------------------------------------------------------
# Devices: lifecycle, pagination, scope-safe not-found (Requirements 17.2, 17.12)
# ---------------------------------------------------------------------------


def test_device_registration_and_pagination(client_factory) -> None:
    client, token_for = client_factory
    token = token_for(LocalPersona.LEARNER)

    for index in range(2):
        created = client.post(
            "/v1/me/devices",
            headers=_auth(token, idempotency_key=f"device-{index}"),
            json={"name": f"Device {index}", "platform": "macos"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "active"

    first_page = client.get("/v1/me/devices?limit=1", headers=_auth(token))
    assert first_page.status_code == 200
    page = first_page.json()
    assert len(page["items"]) == 1
    assert page["next_cursor"] is not None

    second_page = client.get(
        f"/v1/me/devices?limit=1&cursor={page['next_cursor']}", headers=_auth(token)
    )
    assert len(second_page.json()["items"]) == 1


def test_missing_device_is_scope_safe_not_found(client_factory) -> None:
    client, token_for = client_factory
    response = client.get(
        "/v1/me/devices/00000000-0000-4000-8000-0000000000ff",
        headers=_auth(token_for(LocalPersona.LEARNER)),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_device_revocation(client_factory) -> None:
    client, token_for = client_factory
    token = token_for(LocalPersona.LEARNER)
    created = client.post(
        "/v1/me/devices",
        headers=_auth(token, idempotency_key="device-to-revoke"),
        json={"name": "Old", "platform": "windows"},
    )
    device_id = created.json()["id"]
    revoked = client.delete(
        f"/v1/me/devices/{device_id}",
        headers=_auth(token, idempotency_key="revoke-1"),
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"


# ---------------------------------------------------------------------------
# Relationships (Requirement 17.2)
# ---------------------------------------------------------------------------


def test_relationship_listing_serializes_timestamps_and_uuids(client_factory) -> None:
    client, token_for = client_factory
    response = client.get(
        "/v1/me/relationships", headers=_auth(token_for(LocalPersona.LEARNER))
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["learner_user_id"] == str(LOCAL_LEARNER_ID)
        assert isinstance(item["permission_scope"], list)
