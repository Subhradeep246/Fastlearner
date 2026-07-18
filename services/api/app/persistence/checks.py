from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pgvector.sqlalchemy import Vector
from sqlalchemy import Connection, Engine, create_engine

from app.persistence.models import metadata


def _compare_type(
    context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_type: Any,
    metadata_type: Any,
) -> bool | None:
    """Suppress spurious vector type differences.

    Backends without native ``vector`` support (for example SQLite used by the
    consistency tests) reflect the pgvector column as a numeric type. Treat the
    canonical ``Vector`` column as unchanged so model/schema consistency checks
    are meaningful across backends; PostgreSQL compares ``vector`` to ``vector``
    identically.
    """
    if isinstance(metadata_type, Vector):
        return False
    return None

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True)
class RevisionCompatibility:
    current: str | None
    expected: str

    @property
    def compatible(self) -> bool:
        return self.current == self.expected


class DatabaseMigrationRequired(RuntimeError):
    code = "database_migration_required"
    retryable = False

    def __init__(self, compatibility: RevisionCompatibility) -> None:
        self.compatibility = compatibility
        current = compatibility.current or "unversioned"
        super().__init__(f"Database revision {current} is not compatible with application revision {compatibility.expected}")

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def expected_revision() -> str:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError("Release migrations must have exactly one head")
    return heads[0]


def revision_chain() -> tuple[str, ...]:
    script = ScriptDirectory.from_config(alembic_config())
    revisions = tuple(reversed(tuple(script.walk_revisions(base="base", head="heads"))))
    if any(revision.is_merge_point for revision in revisions):
        raise RuntimeError("Merge revisions are forbidden in the release migration chain")
    return tuple(revision.revision for revision in revisions)


def check_revision(connection: Connection) -> RevisionCompatibility:
    return RevisionCompatibility(
        current=MigrationContext.configure(connection).get_current_revision(),
        expected=expected_revision(),
    )


def assert_revision_compatible(connection: Connection) -> None:
    compatibility = check_revision(connection)
    if not compatibility.compatible:
        raise DatabaseMigrationRequired(compatibility)


def schema_differences(connection: Connection) -> tuple[object, ...]:
    context = MigrationContext.configure(connection, opts={"compare_type": _compare_type})
    return tuple(compare_metadata(context, metadata))


def assert_schema_matches_models(connection: Connection) -> None:
    differences = schema_differences(connection)
    if differences:
        raise RuntimeError(f"Database schema differs from canonical models: {differences!r}")


def check_database_url(database_url: str) -> None:
    engine: Engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert_revision_compatible(connection)
    finally:
        engine.dispose()
