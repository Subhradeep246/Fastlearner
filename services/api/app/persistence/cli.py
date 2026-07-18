from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from sqlalchemy import create_engine

from app.config import load_settings
from app.persistence.checks import check_database_url
from app.persistence.seeds import CurriculumManifest, apply_curriculum_manifest, seed_local_personas


def _database_url() -> str:
    settings = load_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required for database commands")
    return settings.database_url.get_secret_value()


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(prog="fastlearner-db")
    subcommands = command_parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check-revision")
    subcommands.add_parser("seed-local-personas")
    curriculum = subcommands.add_parser("seed-curriculum")
    curriculum.add_argument("--manifest", required=True, type=Path)
    return command_parser


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    database_url = _database_url()
    if args.command == "check-revision":
        check_database_url(database_url)
        return 0

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            if args.command == "seed-local-personas":
                seed_local_personas(connection)
            elif args.command == "seed-curriculum":
                apply_curriculum_manifest(connection, CurriculumManifest.load(args.manifest))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
