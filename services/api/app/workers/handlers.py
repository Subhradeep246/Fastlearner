"""Durable job handlers for the graph-memory ingestion and retraction pipeline.

These handlers run inside the :class:`~app.workers.worker.DurableWorker` and are
idempotent because delivery is at least once. Each handler is a small, focused
step wired through the transactional outbox:

* ``source_chunking`` splits an accepted episode into bounded source chunks.
* ``embedding`` fills missing embedding vectors through the AI provider port.
* ``graph_ingestion`` ingests a live canonical episode into the graph store and
  marks its synchronization state ``synced``.
* ``graph_retraction`` retracts a deleted episode from the graph and marks the
  synchronization state ``retracted``.
* ``physical_cleanup`` hard-removes source chunks for a deleted episode.

Failures surface visibly: a graph outage records a failed synchronization state
with an incremented attempt count and re-raises a retryable error, so the
durable worker retries under policy and eventually dead-letters without ever
losing the accepted canonical episode or restoring retrieval eligibility for a
deletion (Requirements 9.10, 16.4, 20.6, 21.3).
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Mapping, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import Engine

from app.clock import Clock, system_clock
from app.domain.graph import (
    ChunkingConfig,
    GraphEpisode,
    GraphMemoryPort,
    GraphScope,
    GraphUnavailableError,
    chunk_text,
    graph_episode_from_metadata,
    parse_iso_timestamp,
    parse_uuid,
)
from app.domain.memory import EpisodeStatus, GraphSyncStatus
from app.repositories.chunks import SqlSourceChunkRepository
from app.repositories.memory import SqlMemoryRepository
from app.repositories.unit_of_work import unit_of_work
from app.workers.policy import JobKind
from app.workers.worker import Handler, JobContext

T = TypeVar("T")


class Embedder(Protocol):
    """The subset of the AI provider port the embedding worker depends on."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async graph/provider call to completion from a sync worker."""
    return asyncio.run(coro)


class GraphMemoryJobHandlers:
    """Idempotent durable handlers for the graph-memory pipeline."""

    def __init__(
        self,
        engine: Engine,
        graph: GraphMemoryPort,
        embedder: Embedder,
        *,
        clock: Clock = system_clock,
        chunking: ChunkingConfig | None = None,
    ) -> None:
        self._engine = engine
        self._graph = graph
        self._embedder = embedder
        self._clock = clock
        self._chunking = chunking or ChunkingConfig()

    # -- registration ------------------------------------------------------

    def handlers(self) -> dict[str, Handler]:
        return {
            JobKind.SOURCE_CHUNKING.value: self.handle_source_chunking,
            JobKind.EMBEDDING.value: self.handle_embedding,
            JobKind.GRAPH_INGESTION.value: self.handle_graph_ingestion,
            JobKind.GRAPH_RETRACTION.value: self.handle_graph_retraction,
            JobKind.PHYSICAL_CLEANUP.value: self.handle_physical_cleanup,
        }

    def register(self, worker: Any) -> None:
        for kind, handler in self.handlers().items():
            worker.register(kind, handler)

    # -- chunking ----------------------------------------------------------

    def handle_source_chunking(self, context: JobContext) -> None:
        owner = context.owner_user_id
        episode_id = _require_uuid(context.payload, "episode_id")
        source_id = _require_uuid(context.payload, "source_id")
        subject_id = parse_uuid(context.payload.get("subject_id"))
        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            chunks_repo = SqlSourceChunkRepository(uow.connection)
            episode = memory.get_episode(owner, episode_id)
            if episode is None or episode.status is not EpisodeStatus.ACTIVE:
                return
            if chunks_repo.has_chunks(owner, source_id):
                return
            chunks = chunk_text(episode.content, self._chunking)
            if not chunks:
                return
            chunks_repo.add_chunks(
                owner,
                source_id,
                subject_id=subject_id,
                episode_id=episode_id,
                chunks=chunks,
                metadata={"episode_id": str(episode_id)},
            )
            uow.commit()

    # -- embedding ---------------------------------------------------------

    def handle_embedding(self, context: JobContext) -> None:
        owner = context.owner_user_id
        source_id = _require_uuid(context.payload, "source_id")
        with unit_of_work(self._engine, self._clock) as uow:
            chunks_repo = SqlSourceChunkRepository(uow.connection)
            pending = chunks_repo.list_missing_embeddings(owner, source_id)
            if not pending:
                return
            vectors = _run(self._embedder.embed([chunk.content for chunk in pending]))
            if len(vectors) != len(pending):
                raise GraphUnavailableError(
                    "The embedding provider returned an unexpected vector count."
                )
            moment = self._clock()
            for chunk, vector in zip(pending, vectors):
                chunks_repo.set_embedding(owner, chunk.chunk_id, vector, moment)
            uow.commit()

    # -- graph ingestion ---------------------------------------------------

    def handle_graph_ingestion(self, context: JobContext) -> None:
        owner = context.owner_user_id
        episode_id = _require_uuid(context.payload, "episode_id")
        source_id = _require_uuid(context.payload, "source_id")

        # Idempotency: an already-synced episode is never re-ingested, so at
        # least once delivery cannot create duplicate graph facts.
        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            episode = memory.get_episode(owner, episode_id)
            state = memory.get_graph_sync_state(owner, episode_id)
            if state is None:
                return
            if state.status is GraphSyncStatus.SYNCED:
                return
            # Only a live, permitted canonical episode is ingested; a deleted or
            # missing episode never produces graph facts (Requirement 9.5).
            if episode is None or episode.status is not EpisodeStatus.ACTIVE:
                return
            graph_group = state.graph_group
            subject_id = parse_uuid(context.payload.get("subject_id")) or episode.subject_id
            visibility = str(context.payload.get("visibility") or episode.visibility)
            confidence = context.payload.get("user_confidence", episode.user_confidence)
            created_at = parse_iso_timestamp(context.payload.get("created_at"))
            content = episode.content

        graph_episode: GraphEpisode = graph_episode_from_metadata(
            owner_id=owner,
            episode_id=episode_id,
            source_id=source_id,
            content=content,
            group=graph_group,
            subject_id=subject_id,
            visibility=visibility,
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            created_at=created_at,
        )

        try:
            _run(self._graph.ingest_episode(graph_episode))
        except GraphUnavailableError as error:
            self._record_failure(owner, episode_id, _error_code(error))
            raise

        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            memory.mark_graph_sync_synced(owner, episode_id, self._clock())
            uow.commit()

    # -- graph retraction --------------------------------------------------

    def handle_graph_retraction(self, context: JobContext) -> None:
        owner = context.owner_user_id
        episode_id = _require_uuid(context.payload, "episode_id")
        subject_id = parse_uuid(context.payload.get("subject_id"))

        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            state = memory.get_graph_sync_state(owner, episode_id)
            if state is None or state.status is GraphSyncStatus.RETRACTED:
                return
            if subject_id is None:
                subject_id = parse_uuid(_group_subject(state.graph_group))

        scope = GraphScope(owner_id=owner, subject_id=subject_id)
        try:
            _run(self._graph.retract_episode(scope, episode_id))
        except GraphUnavailableError as error:
            # A failed retraction never restores retrieval eligibility; it stays
            # visibly failed and remains eligible for retry (Requirement 20.6).
            self._record_failure(owner, episode_id, _error_code(error))
            raise

        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            memory.mark_graph_sync_retracted(owner, episode_id, self._clock())
            uow.commit()

    # -- physical cleanup --------------------------------------------------

    def handle_physical_cleanup(self, context: JobContext) -> None:
        owner = context.owner_user_id
        episode_id = _require_uuid(context.payload, "episode_id")
        with unit_of_work(self._engine, self._clock) as uow:
            chunks_repo = SqlSourceChunkRepository(uow.connection)
            chunks_repo.hard_delete_for_episode(owner, episode_id)
            uow.commit()

    # -- helpers -----------------------------------------------------------

    def _record_failure(self, owner: UUID, episode_id: UUID, error_code: str) -> None:
        with unit_of_work(self._engine, self._clock) as uow:
            memory = SqlMemoryRepository(uow.connection)
            memory.record_graph_sync_failure(owner, episode_id, error_code, self._clock())
            uow.commit()


def _require_uuid(payload: Mapping[str, Any], key: str) -> UUID:
    value = parse_uuid(payload.get(key))
    if value is None:
        raise ValueError(f"Job payload is missing a valid '{key}'")
    return value


def _group_subject(group: str) -> str | None:
    marker = ":subject:"
    index = group.find(marker)
    if index == -1:
        return None
    return group[index + len(marker) :]


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__
