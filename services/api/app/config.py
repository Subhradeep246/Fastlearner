from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_DEFAULT_CREDENTIALS = frozenset(
    {"change-me", "changeme", "dev", "development", "fastlearner", "neo4j", "password", "secret"}
)


class StartupErrorCode(StrEnum):
    MISSING_SETTING = "missing_setting"
    INVALID_SETTING = "invalid_setting"
    FORBIDDEN_SETTING = "forbidden_setting"
    INSECURE_ORIGIN = "insecure_origin"
    DEFAULT_CREDENTIAL = "default_credential"


@dataclass(frozen=True)
class ConfigurationIssue:
    code: StartupErrorCode
    setting: str
    message: str

    def safe_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


class StartupConfigurationError(RuntimeError):
    code = "configuration_error"
    retryable = False

    def __init__(self, issues: tuple[ConfigurationIssue, ...]) -> None:
        self.issues = issues
        names = ", ".join(sorted({issue.setting for issue in issues}))
        super().__init__(f"Runtime configuration is invalid for setting(s): {names}")

    def safe_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "issues": [issue.safe_dict() for issue in self.issues],
        }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["local", "oidc"] = "local"
    api_host: str = "127.0.0.1"
    api_public_url: str = "http://localhost:8000/v1"
    cors_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:1420,http://127.0.0.1:1420,"
        "tauri://localhost,http://tauri.localhost"
    )

    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None

    ai_provider: Literal["openai", "baseten", "disabled"] = "openai"
    ai_model: str | None = None
    ai_api_key: SecretStr | None = None
    embedding_model: str | None = None
    ai_base_url: str = "https://api.openai.com/v1"
    ai_transcription_model: str | None = None
    ai_speech_model: str | None = None
    ai_request_timeout_seconds: float = 30.0
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    app_encryption_key: SecretStr | None = None
    session_signing_secret: SecretStr | None = None

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip())

    def validate_startup(self) -> None:
        issues: list[ConfigurationIssue] = []
        if self.auth_mode == "local" and not _is_loopback_host(self.api_host):
            issues.append(_issue(StartupErrorCode.FORBIDDEN_SETTING, "API_HOST", "Local auth must bind to loopback."))

        if self.environment == "production":
            issues.extend(self._production_issues())

        if issues:
            raise StartupConfigurationError(tuple(issues))

    def _production_issues(self) -> list[ConfigurationIssue]:
        issues: list[ConfigurationIssue] = []
        required: dict[str, object | None] = {
            "DATABASE_URL": self.database_url,
            "REDIS_URL": self.redis_url,
            "NEO4J_URI": self.neo4j_uri,
            "NEO4J_USER": self.neo4j_user,
            "NEO4J_PASSWORD": self.neo4j_password,
            "AI_MODEL": self.ai_model,
            "EMBEDDING_MODEL": self.embedding_model,
            "APP_ENCRYPTION_KEY": self.app_encryption_key,
            "SESSION_SIGNING_SECRET": self.session_signing_secret,
        }
        if self.ai_provider in {"openai", "baseten"}:
            required["AI_API_KEY"] = self.ai_api_key
        for name, value in required.items():
            if _is_missing(value):
                issues.append(_issue(StartupErrorCode.MISSING_SETTING, name, "Required production setting is missing."))

        if self.auth_mode == "local":
            issues.append(_issue(StartupErrorCode.FORBIDDEN_SETTING, "AUTH_MODE", "Local auth is unavailable in production."))
        issues.extend(_origin_issues("API_PUBLIC_URL", (self.api_public_url,)))
        issues.extend(_origin_issues("CORS_ALLOWED_ORIGINS", self.allowed_origins))
        if not self.allowed_origins:
            issues.append(_issue(StartupErrorCode.MISSING_SETTING, "CORS_ALLOWED_ORIGINS", "At least one production origin is required."))
        issues.extend(_credential_issues(self))
        return issues


def _issue(code: StartupErrorCode, setting: str, message: str) -> ConfigurationIssue:
    return ConfigurationIssue(code=code, setting=setting, message=message)


def _secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value().strip() if value is not None else None


def _is_missing(value: object | None) -> bool:
    if value is None:
        return True
    if isinstance(value, SecretStr):
        return not value.get_secret_value().strip()
    return isinstance(value, str) and not value.strip()


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    return host.strip().strip("[]").lower() in {"localhost", "127.0.0.1", "::1"}


def _origin_issues(setting: str, origins: tuple[str, ...]) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    for origin in origins:
        if origin == "*":
            issues.append(_issue(StartupErrorCode.INSECURE_ORIGIN, setting, "Wildcard origins are forbidden in production."))
            continue
        parsed = urlsplit(origin)
        if not parsed.scheme or not parsed.hostname:
            issues.append(_issue(StartupErrorCode.INVALID_SETTING, setting, "Origin must be an absolute URL."))
        elif parsed.scheme != "https" and not _is_loopback_host(parsed.hostname):
            issues.append(_issue(StartupErrorCode.INSECURE_ORIGIN, setting, "Non-loopback production origins must use HTTPS."))
    return issues


def _credential_issues(settings: Settings) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    database_url = _secret_value(settings.database_url)
    if database_url:
        try:
            database_password = urlsplit(database_url).password
        except ValueError:
            issues.append(_issue(StartupErrorCode.INVALID_SETTING, "DATABASE_URL", "Database URL is invalid."))
        else:
            if database_password and database_password.lower() in _DEFAULT_CREDENTIALS:
                issues.append(_issue(StartupErrorCode.DEFAULT_CREDENTIAL, "DATABASE_URL", "Default database credentials are forbidden in production."))

    guarded_secrets = {
        "NEO4J_PASSWORD": _secret_value(settings.neo4j_password),
        "APP_ENCRYPTION_KEY": _secret_value(settings.app_encryption_key),
        "SESSION_SIGNING_SECRET": _secret_value(settings.session_signing_secret),
    }
    for name, value in guarded_secrets.items():
        if value and value.lower() in _DEFAULT_CREDENTIALS:
            issues.append(_issue(StartupErrorCode.DEFAULT_CREDENTIAL, name, "Default credentials are forbidden in production."))
    return issues


def load_settings(**overrides: Any) -> Settings:
    try:
        settings = Settings(**overrides)
    except ValidationError as error:
        names = sorted({str(item["loc"][0]).upper() for item in error.errors()})
        issues = tuple(
            _issue(StartupErrorCode.INVALID_SETTING, name, "Runtime setting has an invalid value.")
            for name in names
        )
        raise StartupConfigurationError(issues) from None
    settings.validate_startup()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
