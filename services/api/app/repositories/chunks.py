"""Source-chunk repository for the graph ingestion pipeline workers.

The chunking, embedding, and physical-cleanup workers persist and remove
``source_chunks`` through this owner-scoped port. Every read and write requires
the resolved owner id as a positional scope argument; the repository never
accepts an unverified request-body owner. All operations run on the
unit-of-work connection so they commit or roll back with the enclosing job
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence
from uuid import UUID, uuid4

from sqlalchemy import Connection, and_, select

from app.domain.graph import TextChunk
from app.persistence.models import require_owner, source_chunks, utc_datetime


@dataclass(frozen=True)
class PendingChunk:
    """A persisted chunk awaiting an embedding vector."""

    chunk_id: UUID
    content: str


class SourceChunkRepository(Protocol):
    """Port for source-chunk persistence used by the ingestion pipeline."""

    def has_chunks(self, owner_user_id: UUID, source_id: UUID) -> bool: ...

    def add_chunks(
        self,
        owner_user_id: UUID,
        source_id: UUID,
        *,
        subject_id: UUID | None,
        episode_id: UUID | None,
        chunks: Sequence[TextChunk],
        metadata: dict[str, Any] | None = None,
    ) -> list[UUID]: ...

    def list_missing_embeddings(
        self, owner_user_id: UUID, source_id: UUID
    ) -> list[PendingChunk]: ...

    def set_embedding(
        self, owner_user_id: UUID, chunk_id: UUID, embedding: Sequence[float], at: datetime
    ) -> None: ...

    def soft_delete_for_episode(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> int: ...

    def hard_delete_for_episode(self, owner_user_id: UUID, episode_id: UUID) -> int: ...


class SqlSourceChunkRepository(SourceChunkRepository):
    """SQLAlchemy Core implementation backed by a live (unit-of-work) connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def has_chunks(self, owner_user_id: UUID, source_id: UUID) -> bool:
        row = self._connection.execute(
            select(source_chunks.c.id)
            .where(
                and_(
                    source_chunks.c.owner_user_id == owner_user_id,
                    source_chunks.c.source_id == source_id,
                    source_chunks.c.deleted_at.is_(None),
                )
            )
            .limit(1)
        ).first()
        return row is not None

    def add_chunks(
        self,
        owner_user_id: UUID,
        source_id: UUID,
        *,
        subject_id: UUID | None,
        episode_id: UUID | None,
        chunks: Sequence[TextChunk],
        metadata: dict[str, Any] | None = None,
    ) -> list[UUID]:
        owner = require_owner(owner_user_id)
        created: list[UUID] = []
        for chunk in chunks:
            chunk_id = uuid4()
            self._connection.execute(
                source_chunks.insert().values(
                    id=chunk_id,
                    owner_user_id=owner,
                    subject_id=subject_id,
                    source_id=source_id,
                    episode_id=episode_id,
                    position=chunk.position,
                    content=chunk.content,
                    embedding=None,
                    metadata_json=dict(metadata or {}),
                    deleted_at=None,
                )
            )
            created.append(chunk_id)
        return created

    def list_missing_embeddings(
        self, owner_user_id: UUID, source_id: UUID
    ) -> list[PendingChunk]:
        rows = self._connection.execute(
            select(source_chunks.c.id, source_chunks.c.content)
            .where(
                and_(
                    source_chunks.c.owner_user_id == owner_user_id,
                    source_chunks.c.source_id == source_id,
                    source_chunks.c.embedding.is_(None),
                    source_chunks.c.deleted_at.is_(None),
                )
            )
            .order_by(source_chunks.c.position)
        ).all()
        return [PendingChunk(chunk_id=row.id, content=row.content) for row in rows]

    def set_embedding(
        self, owner_user_id: UUID, chunk_id: UUID, embedding: Sequence[float], at: datetime
    ) -> None:
        self._connection.execute(
            source_chunks.update()
            .where(
                and_(
                    source_chunks.c.owner_user_id == owner_user_id,
                    source_chunks.c.id == chunk_id,
                )
            )
            .values(embedding=list(embedding), updated_at=utc_datetime(at))
        )

    def soft_delete_for_episode(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> int:
        result = self._connection.execute(
            source_chunks.update()
            .where(
                and_(
                    source_chunks.c.owner_user_id == owner_user_id,
                    source_chunks.c.episode_id == episode_id,
                    source_chunks.c.deleted_at.is_(None),
                )
            )
            .values(deleted_at=utc_datetime(at), updated_at=utc_datetime(at))
        )
        return int(result.rowcount or 0)

    def hard_delete_for_episode(self, owner_user_id: UUID, episode_id: UUID) -> int:
        result = self._connection.execute(
            source_chunks.delete().where(
                and_(
                    source_chunks.c.owner_user_id == owner_user_id,
                    source_chunks.c.episode_id == episode_id,
                )
            )
        )
        return int(result.rowcount or 0)
