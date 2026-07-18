import secrets
from pathlib import Path
from typing import Any

import pytest

from app.devtools import local


def _valid_env(postgres_password: str = "local-postgres-secret") -> str:
    return "\n".join(
        (
            f"POSTGRES_PASSWORD={postgres_password}",
            "NEO4J_PASSWORD=local-neo4j-secret",
            f"DATABASE_URL=postgresql+psycopg://fastlearner:{postgres_password}@localhost:5432/fastlearner",
            "REDIS_URL=redis://localhost:6379/0",
            "NEO4J_URI=bolt://localhost:7687",
            "API_PUBLIC_URL=http://localhost:8000/v1",
        )
    )


def test_read_dotenv_ignores_comments_and_parses_quoted_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# local only\nPOSTGRES_PASSWORD='safe-value'\nEMPTY=\n", encoding="utf-8")

    assert local.read_dotenv(env_file) == {
        "POSTGRES_PASSWORD": "safe-value",
        "EMPTY": "",
    }


def test_missing_configuration_reports_names_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = secrets.token_urlsafe(24)
    env_file = tmp_path / ".env"
    env_file.write_text(
        _valid_env(postgres_password=secret).replace(
            "NEO4J_PASSWORD=local-neo4j-secret", "NEO4J_PASSWORD=<set-password>"
        ),
        encoding="utf-8",
    )
    for name in (
        "POSTGRES_PASSWORD",
        "NEO4J_PASSWORD",
        "DATABASE_URL",
        "REDIS_URL",
        "NEO4J_URI",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(local.LocalDevError) as caught:
        local.local_environment(env_file)

    rendered = caught.value.failure.render()
    assert "NEO4J_PASSWORD" in rendered
    assert secret not in rendered
    assert "<set-password>" not in rendered


def test_startup_commands_preserve_required_readiness_order() -> None:
    assert local.STARTUP_ORDER == (
        "dependencies",
        "migration",
        "seed",
        "worker",
        "API",
        "desktop",
    )
    assert local.startup_order(services_only=True) == local.STARTUP_ORDER[:-1]


def test_destructive_reset_requires_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(local, "preflight", fail_if_called)

    with pytest.raises(local.LocalDevError) as caught:
        local.reset_local_data("yes")

    assert not called
    assert "no data was changed" in caught.value.failure.outcome
    assert local.RESET_CONFIRMATION in caught.value.failure.remediation


def test_command_builders_are_argument_arrays_without_shell_chaining() -> None:
    commands = (
        local.migration_command(),
        local.seed_command(),
        local.worker_command(),
        local.desktop_command(),
    )

    assert all(isinstance(command, list) for command in commands)
    assert all("&&" not in argument and ";" not in argument for command in commands for argument in command)
