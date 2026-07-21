"""Idempotent development seed command.

Applies the stable local personas and the versioned initial mathematics
curriculum pack within a single transaction. Running the command repeatedly is
a no-op after the first successful application, so it is safe for the local
supervisor to invoke on every startup.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from sqlalchemy import Connection, create_engine

from app.config import load_settings
from app.persistence.curriculum_pack import mathematics_manifest
from app.persistence.seeds import (
    apply_curriculum_manifest,
    seed_default_bkt_parameters,
    seed_local_personas,
)


def _database_url() -> str:
    settings = load_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required for seed commands")
    return settings.database_url.get_secret_value()


def seed_all(connection: Connection) -> None:
    """Seed local personas and the initial mathematics curriculum pack."""
    seed_local_personas(connection)
    seed_default_bkt_parameters(connection)
    apply_curriculum_manifest(connection, mathematics_manifest())


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(prog="fastlearner-seed")
    command_parser.add_argument("--profile", default="local", choices=("local",))
    return command_parser


def main(argv: Sequence[str] | None = None) -> int:
    parser().parse_args(argv)
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            seed_all(connection)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
