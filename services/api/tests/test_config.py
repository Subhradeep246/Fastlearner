import json
from typing import Any

import pytest

from app.config import Settings, StartupConfigurationError, StartupErrorCode, load_settings


def _production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "auth_mode": "oidc",
        "api_host": "0.0.0.0",
        "api_public_url": "https://api.fastlearner.example/v1",
        "cors_allowed_origins": "https://app.fastlearner.example",
        "database_url": "postgresql+psycopg://service:unique-db-pass@db:5432/fastlearner",
        "redis_url": "rediss://:unique-redis-pass@queue:6379/0",
        "neo4j_uri": "neo4j+s://graph:7687",
        "neo4j_user": "service-user",
        "neo4j_password": "unique-graph-pass",
        "ai_provider": "openai",
        "ai_model": "configured-language-model",
        "ai_api_key": "server-only-provider-key",
        "embedding_model": "configured-embedding-model",
        "app_encryption_key": "unique-encryption-key",
        "session_signing_secret": "unique-signing-secret",
    }
    values.update(overrides)
    return load_settings(**values)


def test_valid_production_settings_are_accepted() -> None:
    settings = _production_settings()

    assert settings.environment == "production"
    assert settings.allowed_origins == ("https://app.fastlearner.example",)
    assert "unique-db-pass" not in repr(settings)


def test_production_rejects_local_auth_and_missing_secrets_safely() -> None:
    with pytest.raises(StartupConfigurationError) as caught:
        _production_settings(auth_mode="local", ai_api_key=None, session_signing_secret=None)

    issues = {(issue.code, issue.setting) for issue in caught.value.issues}
    assert (StartupErrorCode.FORBIDDEN_SETTING, "AUTH_MODE") in issues
    assert (StartupErrorCode.MISSING_SETTING, "AI_API_KEY") in issues
    assert (StartupErrorCode.MISSING_SETTING, "SESSION_SIGNING_SECRET") in issues
    assert "server-only-provider-key" not in json.dumps(caught.value.safe_payload())


def test_production_rejects_insecure_origins_and_default_credentials() -> None:
    with pytest.raises(StartupConfigurationError) as caught:
        _production_settings(
            api_public_url="http://api.fastlearner.example/v1",
            cors_allowed_origins="*,http://app.fastlearner.example",
            database_url="postgresql+psycopg://service:password@db:5432/fastlearner",
            neo4j_password="change-me",
        )

    issues = {(issue.code, issue.setting) for issue in caught.value.issues}
    assert (StartupErrorCode.INSECURE_ORIGIN, "API_PUBLIC_URL") in issues
    assert (StartupErrorCode.INSECURE_ORIGIN, "CORS_ALLOWED_ORIGINS") in issues
    assert (StartupErrorCode.DEFAULT_CREDENTIAL, "DATABASE_URL") in issues
    assert (StartupErrorCode.DEFAULT_CREDENTIAL, "NEO4J_PASSWORD") in issues


def test_local_auth_cannot_bind_to_a_non_loopback_interface() -> None:
    with pytest.raises(StartupConfigurationError) as caught:
        load_settings(environment="development", auth_mode="local", api_host="0.0.0.0")

    issue = caught.value.issues[0]
    assert issue.code == StartupErrorCode.FORBIDDEN_SETTING
    assert issue.setting == "API_HOST"
