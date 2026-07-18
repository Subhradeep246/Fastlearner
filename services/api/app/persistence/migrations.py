from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection
from sqlalchemy.schema import CreateIndex, CreateTable, DropIndex, DropTable

from app.persistence.models import TABLE_GROUPS, metadata


def create_group(bind: Connection, group: str) -> None:
    names = set(TABLE_GROUPS[group])
    for table in metadata.sorted_tables:
        if table.name not in names:
            continue
        inline_foreign_keys = {
            constraint for constraint in table.foreign_key_constraints if not constraint.use_alter
        }
        bind.execute(
            CreateTable(table, include_foreign_key_constraints=inline_foreign_keys)
        )


def drop_group(bind: Connection, group: str) -> None:
    names = set(TABLE_GROUPS[group])
    for table in reversed(metadata.sorted_tables):
        if table.name in names:
            bind.execute(DropTable(table))


def create_indexes(bind: Connection, owner: bool) -> None:
    for index in _indexes(owner):
        bind.execute(CreateIndex(index))


def drop_indexes(bind: Connection, owner: bool) -> None:
    for index in reversed(tuple(_indexes(owner))):
        bind.execute(DropIndex(index))


def _indexes(owner: bool) -> Iterable[object]:
    for table in metadata.sorted_tables:
        for index in sorted(table.indexes, key=lambda value: value.name or ""):
            if (index.info.get("migration_group") == "owner") is owner:
                yield index
