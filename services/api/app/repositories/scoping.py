"""Owner-scoped query helpers.

Every learner-data query must carry an authenticated owner predicate so a
repository can only ever read or write rows inside the resolved owner scope
(Requirement 19.9). These helpers centralize that predicate so it cannot be
accidentally omitted, and they reject a missing owner scope before a statement
is ever built.

Auth-resolution queries that must run *before* an owner scope exists (looking a
session up by id, checking that a user exists, or finding the observer
relationship that resolves the owner) are intentionally not routed through these
helpers; they never return learner-owned rows.
"""

from __future__ import annotations

from uuid import UUID

from typing import Any

from sqlalchemy import ColumnElement, Select, Table, and_, select

from app.persistence.models import require_owner


def owner_predicate(table: Table, owner_user_id: UUID | None) -> ColumnElement[bool]:
    """Return ``table.owner_user_id == <resolved owner>``.

    Raises ``ValueError`` when the owner scope is missing so a query can never
    be issued without an authenticated owner, and ``KeyError`` when the table
    is not owner-scoped so misuse is caught immediately.
    """
    if "owner_user_id" not in table.c:
        raise KeyError(f"Table '{table.name}' has no owner_user_id column to scope by")
    return table.c.owner_user_id == require_owner(owner_user_id)


def owner_scoped_select(
    table: Table,
    owner_user_id: UUID | None,
    *conditions: ColumnElement[bool],
) -> Select[Any]:
    """Build a ``SELECT * FROM table`` that always includes the owner predicate.

    Additional ``conditions`` (for example an ``id`` match) are ANDed with the
    mandatory owner predicate, so scope-safe absence falls out naturally: a row
    owned by another scope simply does not match.
    """
    return select(table).where(and_(owner_predicate(table, owner_user_id), *conditions))
