"""Deterministic serialization helpers for the versioned API.

All API payloads use UTC RFC 3339 timestamps, UUID strings, and opaque cursor
pagination (Requirement 17). Timestamps are normalized to timezone-aware UTC and
rendered with a trailing ``Z`` so clients receive an unambiguous instant.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Callable, Generic, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, field_serializer

T = TypeVar("T")

#: Default and maximum page sizes for cursor-paginated collections.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def to_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    Naive datetimes are assumed to already be UTC (the persistence layer stores
    UTC instants); aware datetimes are converted to UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Render a datetime as an RFC 3339 UTC string with a trailing ``Z``."""
    normalized = to_utc(value)
    return normalized.isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    """Base model for API payloads with deterministic time serialization."""

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_datetimes(self, value: object) -> object:
        if isinstance(value, datetime):
            return to_rfc3339(value)
        return value


def encode_cursor(value: str) -> str:
    """Encode an opaque forward cursor from a stable ordering key."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> str:
    """Decode an opaque forward cursor, raising ``ValueError`` when malformed."""
    padding = "=" * (-len(cursor) % 4)
    return base64.urlsafe_b64decode(cursor + padding).decode("utf-8")


class CursorPage(ApiModel, Generic[T]):
    """A single page of a cursor-paginated collection.

    ``next_cursor`` is ``None`` when no further records remain in the authorized
    scope. Empty collections yield an empty ``items`` list and a ``None`` cursor.
    """

    items: list[T]
    next_cursor: str | None = None


def paginate(
    records: Sequence[T],
    *,
    key: Callable[[T], str],
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[T], str | None]:
    """Apply forward cursor pagination over a stably ordered sequence.

    ``key`` derives the opaque ordering token for each record. When ``cursor`` is
    supplied, only records after the matching key are returned. A ``next_cursor``
    is emitted only when additional records remain after the returned page.
    """
    bounded_limit = max(1, min(limit, MAX_PAGE_SIZE))
    start = 0
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("Pagination cursor is malformed.") from error
        start = next(
            (index + 1 for index, record in enumerate(records) if key(record) == after),
            len(records),
        )
    window = list(records[start : start + bounded_limit])
    has_more = start + bounded_limit < len(records)
    next_cursor = encode_cursor(key(window[-1])) if window and has_more else None
    return window, next_cursor
