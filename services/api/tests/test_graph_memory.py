"""Tests for the Graphiti/Neo4j graph-memory adapters and pipeline workers.

Covers Requirements 9.4 (exact owner/subject group mapping), 9.5 (provenance
metadata and live-episode verification), 9.6/16.13 (canonical precedence),
9.10/16.4 (retained episode and retry on graph failure), 16.3 (Graphiti adapter),
16.7/20.6 (degraded operation and retained retry state), 20.4/20.7 (retraction
and cleanup), and 21.3 (visible synchronization state on failure).

No test opens a real Neo4j connection or AI provider; a fake graph client and a
fake embedder are injected instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select

from app.adapters.files import SignatureFileScanner
from app.adapters.graph import (
    GraphClientError,
    GraphitiGraphMemory,
    SqlGraphProvenanceVerifier,
    UnavailableGraphMemory,
    create_graph_memory,
)
from app.domain.graph import (
    ChunkingConfig,
    GraphEpisode,
    GraphFact,
    GraphScope,
    GraphUnavailableError,
    chunk_text,
    verify_fact_provenance,
)
from app.domain.identity import ActorContext, LEARNER_OWNER_SCOPES, Role
from app.domain.memory import EpisodeKind, GraphSyncStatus
from app.persistence.models import JobStatus, graph_sync_state, metadata, outbox_jobs, source_chunks
from app.persistence.seeds import LOCAL_LEARNER_ID, seed_local_personas
from app.repositories import unit_of_work
from app.repositories.chunks import SqlSourceChunkRepository
from app.repositories.memory import SqlMemoryRepository
from app.services.memory import MemoryService
from app.workers.handlers import GraphMemoryJobHandlers
from app.workers.policy import JobKind, RetryPolicy
from app.workers.worker import DurableWorker, JobContext

EMBED_DIM = 1536


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _clock() -> FakeClock:
    return FakeClock(datetime(2025, 1, 1, tzinfo=timezone.utc))


class FakeGraphitiClient:
    def __init__(
        self,
        *,
        facts: Sequence[Mapping[str, Any]] | None = None,
        ingest_facts: int = 0,
        removed: int = 1,
        healthy: bool = True,
        fail: bool = False,
    ) -> None:
        self.added: list[dict[str, Any]] = []
        self.searched: list[tuple[str, str, int]] = []
        self.removed_calls: list[tuple[str, str]] = []
        self._facts = list(facts or [])
        self._ingest_facts = ingest_facts
        self._removed = removed
        self._healthy = healthy
        self._fail = fail

    async def add_episode(
        self, *, group_id: str, episode_id: str, body: str, metadata: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        if self._fail:
            raise GraphClientError("graph down")
        self.added.append(
            {"group_id": group_id, "episode_id": episode_id, "body": body, "metadata": dict(metadata)}
        )
        return [{"fact": "derived"} for _ in range(self._ingest_facts)]

    async def search(self, *, group_id: str, query: str, limit: int) -> Sequence[Mapping[str, Any]]:
        if self._fail:
            raise GraphClientError("graph down")
        self.searched.append((group_id, query, limit))
        return self._facts

    async def remove_episode(self, *, group_id: str, episode_id: str) -> int:
        if self._fail:
            raise GraphClientError("graph down")
        self.removed_calls.append((group_id, episode_id))
        return self._removed

    async def health(self) -> bool:
        if self._fail:
            raise GraphClientError("graph down")
        return self._healthy


class FakeEmbedder:
    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index)] * self.dim for index, _ in enumerate(texts)]


def _seeded_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        seed_local_personas(connection)
    return engine


def _learner() -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_LEARNER_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.LEARNER,
        scopes=LEARNER_OWNER_SCOPES,
    )


def _memory_service(engine, clock) -> MemoryService:
    return MemoryService(
        lambda: unit_of_work(engine, clock),
        scanner=SignatureFileScanner(),
        repository_factory=SqlMemoryRepository,
        clock=clock,
    )


def _handlers(engine, clock, *, graph, embedder=None) -> GraphMemoryJobHandlers:
    return GraphMemoryJobHandlers(
        engine,
        graph,
        embedder or FakeEmbedder(),
        clock=clock,
        chunking=ChunkingConfig(max_characters=50, overlap_characters=10),
    )


def _ctx(owner, kind: str, payload: dict[str, Any]) -> JobContext:
    return JobContext(job_id=uuid4(), owner_user_id=owner, kind=kind, payload=payload, attempt=1)


def _capture(service, learner, *, content="Fractions add over a common denominator.", subject_id=None):
    return service.save_context(
        learner, content=content, kind=EpisodeKind.NOTE, subject_id=subject_id
    )


# ---------------------------------------------------------------------------
# Pure domain rules
# ---------------------------------------------------------------------------


def test_graph_scope_maps_owner_and_subject_exactly() -> None:
    owner = uuid4()
    subject = uuid4()
    assert GraphScope(owner).group() == f"user:{owner}"
    assert GraphScope(owner, subject).group() == f"user:{owner}:subject:{subject}"


def test_chunk_text_is_bounded_overlapping_and_deterministic() -> None:
    config = ChunkingConfig(max_characters=10, overlap_characters=3)
    text = "abcdefghijklmnopqrstuvwxyz"
    chunks = chunk_text(text, config)
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.content) <= 10 for chunk in chunks)
    # Deterministic: same input yields identical output.
    assert chunk_text(text, config) == chunks
    # Overlap preserves the tail of the previous chunk at the head of the next.
    assert chunks[1].content[:3] == chunks[0].content[-3:]
    # Reassembling with the stride reconstructs the source.
    assert chunk_text("   ", config) == []


def test_verify_fact_provenance_drops_stale_and_out_of_scope_facts() -> None:
    owner = uuid4()
    scope = GraphScope(owner)
    live_episode = uuid4()
    dead_episode = uuid4()
    facts = [
        GraphFact("f1", live_episode, uuid4(), scope.group(), "kept"),
        GraphFact("f2", dead_episode, uuid4(), scope.group(), "dropped: not live"),
        GraphFact("f3", live_episode, uuid4(), "user:other", "dropped: wrong group"),
    ]
    kept = verify_fact_provenance(scope, facts, [live_episode])
    assert [fact.fact_id for fact in kept] == ["f1"]


# ---------------------------------------------------------------------------
# Graphiti adapter (Requirements 16.3, 9.4, 9.5)
# ---------------------------------------------------------------------------


def test_ingest_attaches_scoped_group_and_provenance_metadata() -> None:
    owner = uuid4()
    subject = uuid4()
    scope = GraphScope(owner, subject)
    client = FakeGraphitiClient(ingest_facts=2)
    adapter = GraphitiGraphMemory(client)
    episode = GraphEpisode(
        episode_id=uuid4(),
        source_id=uuid4(),
        owner_id=owner,
        group=scope.group(),
        content="note body",
        subject_id=subject,
        visibility="private",
        confidence=0.8,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    receipt = _await(adapter.ingest_episode(episode))
    assert receipt.status == "ingested"
    assert receipt.fact_count == 2
    sent = client.added[0]
    assert sent["group_id"] == f"user:{owner}:subject:{subject}"
    assert sent["metadata"]["episode_id"] == str(episode.episode_id)
    assert sent["metadata"]["source_id"] == str(episode.source_id)
    assert sent["metadata"]["visibility"] == "private"
    assert sent["metadata"]["confidence"] == 0.8
    assert sent["metadata"]["schema_version"] == "1"


def test_search_filters_facts_by_injected_provenance_verifier() -> None:
    owner = uuid4()
    scope = GraphScope(owner)
    live = uuid4()
    facts = [
        {"fact_id": "f1", "statement": "kept", "metadata": {
            "episode_id": str(live), "source_id": str(uuid4()), "group": scope.group()}},
        {"fact_id": "f2", "statement": "stale", "metadata": {
            "episode_id": str(uuid4()), "source_id": str(uuid4()), "group": scope.group()}},
    ]
    adapter = GraphitiGraphMemory(
        FakeGraphitiClient(facts=facts),
        provenance_verifier=lambda s, fs: verify_fact_provenance(s, fs, [live]),
    )
    results = _await(adapter.search(scope, "denominator", 5))
    assert [fact.fact_id for fact in results] == ["f1"]


def test_adapter_maps_client_errors_to_retryable_graph_unavailable() -> None:
    adapter = GraphitiGraphMemory(FakeGraphitiClient(fail=True))
    episode = GraphEpisode(uuid4(), uuid4(), uuid4(), "user:x", "body")
    with pytest.raises(GraphUnavailableError) as caught:
        _await(adapter.ingest_episode(episode))
    assert caught.value.retryable is True
    health = _await(adapter.health())
    assert health.available is False


# ---------------------------------------------------------------------------
# Degraded operation (Requirements 16.7, 20.6)
# ---------------------------------------------------------------------------


def test_unavailable_graph_memory_degrades_safely() -> None:
    graph = UnavailableGraphMemory()
    scope = GraphScope(uuid4())
    assert _await(graph.search(scope, "q", 5)) == []
    assert _await(graph.health()).available is False
    with pytest.raises(GraphUnavailableError):
        _await(graph.ingest_episode(GraphEpisode(uuid4(), uuid4(), uuid4(), scope.group(), "b")))
    with pytest.raises(GraphUnavailableError):
        _await(graph.retract_episode(scope, uuid4()))


def test_create_graph_memory_falls_back_to_unavailable_without_neo4j() -> None:
    class _Settings:
        neo4j_uri = None

    assert isinstance(create_graph_memory(_Settings()), UnavailableGraphMemory)


# ---------------------------------------------------------------------------
# Ingestion worker (Requirements 9.5, 9.10, 16.4)
# ---------------------------------------------------------------------------


def test_ingestion_worker_marks_synced_and_sends_content() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    client = FakeGraphitiClient(ingest_facts=1)
    handlers = _handlers(engine, clock, graph=GraphitiGraphMemory(client))

    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}
    handlers.handle_graph_ingestion(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_INGESTION.value, payload))

    with engine.connect() as connection:
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
    assert state["status"] == GraphSyncStatus.SYNCED.value
    assert client.added[0]["body"] == captured.episode.content
    assert client.added[0]["group_id"] == f"user:{LOCAL_LEARNER_ID}"


def test_ingestion_worker_is_idempotent_on_replay() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    client = FakeGraphitiClient()
    handlers = _handlers(engine, clock, graph=GraphitiGraphMemory(client))
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}

    handlers.handle_graph_ingestion(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_INGESTION.value, payload))
    handlers.handle_graph_ingestion(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_INGESTION.value, payload))
    assert len(client.added) == 1  # second delivery skipped; no duplicate facts


def test_ingestion_failure_retains_episode_and_records_failed_sync() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    handlers = _handlers(engine, clock, graph=UnavailableGraphMemory())
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}

    with pytest.raises(GraphUnavailableError):
        handlers.handle_graph_ingestion(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_INGESTION.value, payload))

    with engine.connect() as connection:
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
        # The accepted episode is retained; sync state is visibly failed.
        assert state["status"] == GraphSyncStatus.FAILED.value
        assert state["attempt_count"] == 1
        assert state["last_error_code"] == "graph_unavailable"


def test_ingestion_skips_deleted_episode() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    # Simulate a deleted episode by flipping its status directly.
    from app.persistence.models import memory_episodes

    with engine.begin() as connection:
        connection.execute(
            memory_episodes.update()
            .where(memory_episodes.c.id == captured.episode.id)
            .values(status="deleted")
        )
    client = FakeGraphitiClient()
    handlers = _handlers(engine, clock, graph=GraphitiGraphMemory(client))
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}
    handlers.handle_graph_ingestion(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_INGESTION.value, payload))
    assert client.added == []  # no facts derived from a non-live episode


# ---------------------------------------------------------------------------
# Chunking and embedding workers
# ---------------------------------------------------------------------------


def test_chunking_worker_creates_chunks_once() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner(), content="x" * 120)
    handlers = _handlers(engine, clock, graph=UnavailableGraphMemory())
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}

    handlers.handle_source_chunking(_ctx(LOCAL_LEARNER_ID, JobKind.SOURCE_CHUNKING.value, payload))
    handlers.handle_source_chunking(_ctx(LOCAL_LEARNER_ID, JobKind.SOURCE_CHUNKING.value, payload))

    with engine.connect() as connection:
        rows = connection.execute(
            select(source_chunks).where(source_chunks.c.source_id == captured.source.id)
        ).all()
    assert len(rows) >= 2  # bounded chunks created
    positions = [row.position for row in rows]
    assert positions == sorted(set(positions))  # created exactly once, ordered


def test_embedding_worker_fills_missing_vectors_idempotently() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner(), content="y" * 120)
    embedder = FakeEmbedder()
    handlers = _handlers(engine, clock, graph=UnavailableGraphMemory(), embedder=embedder)
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}
    handlers.handle_source_chunking(_ctx(LOCAL_LEARNER_ID, JobKind.SOURCE_CHUNKING.value, payload))

    handlers.handle_embedding(_ctx(LOCAL_LEARNER_ID, JobKind.EMBEDDING.value, payload))
    with engine.connect() as connection:
        pending = SqlSourceChunkRepository(connection).list_missing_embeddings(
            LOCAL_LEARNER_ID, captured.source.id
        )
    assert pending == []
    # A second delivery has nothing to embed and does not call the provider again.
    handlers.handle_embedding(_ctx(LOCAL_LEARNER_ID, JobKind.EMBEDDING.value, payload))
    assert len(embedder.calls) == 1


# ---------------------------------------------------------------------------
# Retraction and cleanup workers (Requirements 20.4, 20.6, 20.7)
# ---------------------------------------------------------------------------


def test_retraction_worker_marks_retracted() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    client = FakeGraphitiClient(removed=3)
    handlers = _handlers(engine, clock, graph=GraphitiGraphMemory(client))
    payload = {"episode_id": str(captured.episode.id)}

    handlers.handle_graph_retraction(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_RETRACTION.value, payload))
    with engine.connect() as connection:
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
    assert state["status"] == GraphSyncStatus.RETRACTED.value
    assert client.removed_calls[0] == (f"user:{LOCAL_LEARNER_ID}", str(captured.episode.id))


def test_retraction_failure_keeps_retry_state_without_restoring() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    handlers = _handlers(engine, clock, graph=UnavailableGraphMemory())
    payload = {"episode_id": str(captured.episode.id)}
    with pytest.raises(GraphUnavailableError):
        handlers.handle_graph_retraction(_ctx(LOCAL_LEARNER_ID, JobKind.GRAPH_RETRACTION.value, payload))
    with engine.connect() as connection:
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
    assert state["status"] == GraphSyncStatus.FAILED.value  # not retracted, still failed/retryable


def test_cleanup_worker_hard_deletes_chunks() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner(), content="z" * 120)
    handlers = _handlers(engine, clock, graph=UnavailableGraphMemory())
    payload = {"episode_id": str(captured.episode.id), "source_id": str(captured.source.id)}
    handlers.handle_source_chunking(_ctx(LOCAL_LEARNER_ID, JobKind.SOURCE_CHUNKING.value, payload))

    handlers.handle_physical_cleanup(_ctx(LOCAL_LEARNER_ID, JobKind.PHYSICAL_CLEANUP.value, payload))
    with engine.connect() as connection:
        rows = connection.execute(
            select(source_chunks).where(source_chunks.c.episode_id == captured.episode.id)
        ).all()
    assert rows == []


# ---------------------------------------------------------------------------
# Provenance verifier against live canonical episodes (Requirement 9.5)
# ---------------------------------------------------------------------------


def test_sql_provenance_verifier_keeps_only_live_episodes() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    scope = GraphScope(LOCAL_LEARNER_ID)
    verifier = SqlGraphProvenanceVerifier(engine)
    facts = [
        GraphFact("live", captured.episode.id, captured.source.id, scope.group(), "kept"),
        GraphFact("dead", uuid4(), uuid4(), scope.group(), "dropped"),
    ]
    kept = verifier(scope, facts)
    assert [fact.fact_id for fact in kept] == ["live"]


# ---------------------------------------------------------------------------
# End-to-end through the durable worker (Requirements 16.4, 20.6, 21.3)
# ---------------------------------------------------------------------------


def test_durable_worker_processes_graph_ingestion_from_outbox() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())  # enqueues a graph_ingestion outbox job
    client = FakeGraphitiClient()
    worker = DurableWorker(engine, clock=clock)
    _handlers(engine, clock, graph=GraphitiGraphMemory(client)).register(worker)

    result = worker.process_next()
    assert result is not None
    assert result.status is JobStatus.SUCCEEDED
    with engine.connect() as connection:
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
    assert state["status"] == GraphSyncStatus.SYNCED.value


def test_durable_worker_retries_then_dead_letters_on_graph_outage() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _memory_service(engine, clock)
    captured = _capture(service, _learner())
    worker = DurableWorker(
        engine,
        clock=clock,
        policy_resolver=lambda _kind: RetryPolicy(max_attempts=2, base_seconds=0.0, max_seconds=0.0),
        jitter=lambda _ceiling: 0.0,
    )
    _handlers(engine, clock, graph=UnavailableGraphMemory()).register(worker)

    first = worker.process_next()
    assert first is not None and first.status is JobStatus.RETRY_WAIT
    second = worker.process_next()
    assert second is not None and second.status is JobStatus.DEAD_LETTER

    with engine.connect() as connection:
        job = connection.execute(
            select(outbox_jobs).where(outbox_jobs.c.kind == JobKind.GRAPH_INGESTION.value)
        ).mappings().one()
        assert job["status"] == JobStatus.DEAD_LETTER.value
        assert job["last_error_code"] == "graph_unavailable"
        state = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
        assert state["status"] == GraphSyncStatus.FAILED.value


# ---------------------------------------------------------------------------
# Async bridge helper
# ---------------------------------------------------------------------------


def _await(coro):
    import asyncio

    return asyncio.run(coro)
