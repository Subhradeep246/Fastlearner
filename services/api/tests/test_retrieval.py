"""Unit and integration tests for owner-filtered retrieval and bounded ranking.

Covers Requirements 9.7, 9.8, 10.2, 10.3, 10.4, 10.5, 10.6, 10.9, 10.10, and
22.4: exact owner/subject/date/lifecycle filtering before similarity ranking,
canonical-first deterministic ranking, deduplication, record/token bounding,
empty-result scope preservation, and explicit degraded supplementary status.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.adapters.files import SignatureFileScanner
from app.domain.identity import ActorContext, AuthorizationError, LEARNER_OWNER_SCOPES, Role
from app.domain.retrieval import (
    EvidenceKind,
    RankingWeights,
    RetrievalFilters,
    RetrievalItem,
    RetrievalLimits,
    SupplementaryUnavailableError,
    UnavailableGraphRetrieval,
    assemble_context,
    bound,
    cosine_similarity,
    deduplicate,
    estimate_tokens,
    make_item,
    rank,
)
from app.persistence.models import (
    memory_retrieval_log,
    metadata,
    source_chunks,
    sources,
    subjects,
)
from app.persistence.seeds import LOCAL_LEARNER_ID, LOCAL_PARENT_ID, seed_local_personas
from app.repositories import unit_of_work
from app.repositories.memory import SqlMemoryRepository
from app.services.memory import MemoryService

_DIM = 1536


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _clock() -> FakeClock:
    return FakeClock(datetime(2025, 1, 1, tzinfo=timezone.utc))


def _emb(*leading: float) -> list[float]:
    vec = [0.0] * _DIM
    for index, value in enumerate(leading):
        vec[index] = value
    return vec


def _at(day: int) -> datetime:
    return datetime(2025, 1, day, tzinfo=timezone.utc)


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


def _observer(scopes: frozenset[str] = frozenset()) -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_PARENT_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.PARENT,
        scopes=scopes,
    )


def _service(engine, clock, *, graph_retrieval=None, retrieval_limits=None) -> MemoryService:
    return MemoryService(
        lambda: unit_of_work(engine, clock),
        scanner=SignatureFileScanner(),
        repository_factory=SqlMemoryRepository,
        clock=clock,
        graph_retrieval=graph_retrieval,
        retrieval_limits=retrieval_limits,
    )


def _make_subject(engine, owner: UUID, slug: str) -> UUID:
    subject_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            subjects.insert().values(
                id=subject_id,
                owner_user_id=owner,
                slug=slug,
                title=slug.title(),
                kind="learner_created",
            )
        )
    return subject_id


def _make_source(engine, owner: UUID, subject_id: UUID | None, *, status: str = "active", deleted_at=None) -> UUID:
    source_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sources.insert().values(
                id=source_id,
                owner_user_id=owner,
                subject_id=subject_id,
                kind="manual_entry",
                content_checksum="checksum",
                provenance={},
                status=status,
                deleted_at=deleted_at,
            )
        )
    return source_id


def _make_chunk(
    engine,
    owner: UUID,
    source_id: UUID,
    *,
    subject_id: UUID | None = None,
    position: int = 0,
    content: str = "chunk",
    embedding: list[float] | None = None,
    created_at: datetime | None = None,
    deleted_at=None,
) -> UUID:
    chunk_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            source_chunks.insert().values(
                id=chunk_id,
                owner_user_id=owner,
                subject_id=subject_id,
                source_id=source_id,
                episode_id=None,
                position=position,
                content=content,
                embedding=embedding if embedding is not None else _emb(1.0),
                metadata_json={},
                created_at=created_at or _at(1),
                deleted_at=deleted_at,
            )
        )
    return chunk_id


# ---------------------------------------------------------------------------
# Pure ranking rules
# ---------------------------------------------------------------------------


def test_estimate_tokens_is_conservative() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_cosine_similarity_maps_into_unit_interval() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.5)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def _item(kind: EvidenceKind, ref: str, *, relevance=0.5, day=1, dedup=None, **kwargs) -> RetrievalItem:
    return make_item(
        kind=kind,
        ref_id=ref,
        dedup_key=dedup,
        content=kwargs.pop("content", "content"),
        relevance=relevance,
        recorded_at=_at(day),
        **kwargs,
    )


def test_deduplicate_keeps_highest_relevance() -> None:
    low = _item(EvidenceKind.SOURCE_CHUNK, "a", relevance=0.2, dedup="k")
    high = _item(EvidenceKind.SOURCE_CHUNK, "b", relevance=0.9, dedup="k")
    result = deduplicate([low, high])
    assert len(result) == 1
    assert result[0].ref_id == "b"


def test_rank_orders_canonical_before_supplementary() -> None:
    canonical = _item(EvidenceKind.CANONICAL, "c", relevance=0.1, dedup="c")
    strong_chunk = _item(EvidenceKind.SOURCE_CHUNK, "s", relevance=1.0, dedup="s")
    fact = _item(EvidenceKind.GRAPH_FACT, "g", relevance=1.0, dedup="g")
    ordered = rank([strong_chunk, fact, canonical])
    assert [item.kind for item in ordered] == [
        EvidenceKind.CANONICAL,
        EvidenceKind.SOURCE_CHUNK,
        EvidenceKind.GRAPH_FACT,
    ]


def test_rank_is_deterministic_for_equal_inputs() -> None:
    items = [
        _item(EvidenceKind.SOURCE_CHUNK, "b", relevance=0.5, dedup="b"),
        _item(EvidenceKind.SOURCE_CHUNK, "a", relevance=0.5, dedup="a"),
    ]
    first = [item.ref_id for item in rank(items)]
    second = [item.ref_id for item in rank(list(reversed(items)))]
    assert first == second


def test_bound_enforces_record_and_token_limits() -> None:
    items = [
        _item(EvidenceKind.SOURCE_CHUNK, str(index), relevance=0.5, dedup=str(index), content="x" * 40)
        for index in range(10)
    ]
    limits = RetrievalLimits(max_records=3, max_source_chunks=8, max_total_tokens=1000, max_chars_per_item=2000)
    selected, tokens, dropped = bound(items, limits)
    assert len(selected) == 3
    assert dropped == 7
    assert tokens == sum(item.token_estimate for item in selected)


def test_bound_truncates_content_to_char_budget() -> None:
    item = _item(EvidenceKind.SOURCE_CHUNK, "a", dedup="a", content="y" * 5000)
    limits = RetrievalLimits(max_chars_per_item=100)
    selected, _tokens, _dropped = bound([item], limits)
    assert len(selected[0].content) == 100


def test_bound_enforces_per_kind_graph_cap() -> None:
    facts = [
        _item(EvidenceKind.GRAPH_FACT, str(index), dedup=str(index), graph_related=True)
        for index in range(5)
    ]
    limits = RetrievalLimits(max_graph_facts=2)
    selected, _tokens, dropped = bound(facts, limits)
    assert len(selected) == 2
    assert dropped == 3


def test_assemble_context_reports_degraded_supplementary_status() -> None:
    context = assemble_context(
        canonical=[_item(EvidenceKind.CANONICAL, "c", dedup="c")],
        vector_available=True,
        graph_available=False,
    )
    assert context.status.is_degraded is True
    assert "graph_unavailable" in context.status.degraded_reasons
    assert context.status.supplementary_available is False


def test_assemble_context_empty_is_flagged() -> None:
    context = assemble_context()
    assert context.is_empty is True
    assert context.status.supplementary_available is True


# ---------------------------------------------------------------------------
# Service integration: owner-filtered retrieval
# ---------------------------------------------------------------------------


def test_retrieve_context_returns_owner_scoped_chunks_ranked_by_similarity() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, position=0, content="aligned", embedding=_emb(1.0, 0.0))
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, position=1, content="orthogonal", embedding=_emb(0.0, 1.0))

    context = service.retrieve_context(_learner(), query="fractions", query_embedding=_emb(1.0, 0.0))

    assert not context.is_empty
    assert [item.content for item in context.items] == ["aligned", "orthogonal"]
    assert context.items[0].relevance > context.items[1].relevance


def test_retrieve_context_excludes_other_owner_chunks() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    other_owner = uuid4()
    with engine.begin() as connection:
        from app.persistence.models import users

        connection.execute(users.insert().values(id=other_owner, email="x@y.z", display_name="Other"))
    foreign_source = _make_source(engine, other_owner, None)
    _make_chunk(engine, other_owner, foreign_source, content="foreign")

    context = service.retrieve_context(_learner(), query="q", query_embedding=_emb(1.0))
    assert context.is_empty


def test_retrieve_context_applies_subject_filter_before_ranking() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    subject_a = _make_subject(engine, LOCAL_LEARNER_ID, "math")
    subject_b = _make_subject(engine, LOCAL_LEARNER_ID, "science")
    source_a = _make_source(engine, LOCAL_LEARNER_ID, subject_a)
    source_b = _make_source(engine, LOCAL_LEARNER_ID, subject_b)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_a, subject_id=subject_a, content="math", embedding=_emb(0.0, 1.0))
    # Subject B chunk is a *stronger* similarity match but must be excluded.
    _make_chunk(engine, LOCAL_LEARNER_ID, source_b, subject_id=subject_b, content="science", embedding=_emb(1.0, 0.0))

    context = service.retrieve_context(
        _learner(),
        query="q",
        query_embedding=_emb(1.0, 0.0),
        filters=RetrievalFilters(permitted_subject_ids=frozenset({subject_a})),
    )
    assert [item.content for item in context.items] == ["math"]


def test_retrieve_context_empty_subject_set_returns_empty_without_broadening() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, content="present")

    context = service.retrieve_context(
        _learner(),
        query="q",
        query_embedding=_emb(1.0),
        filters=RetrievalFilters(permitted_subject_ids=frozenset()),
    )
    assert context.is_empty


def test_retrieve_context_applies_date_filter() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, position=0, content="old", created_at=_at(1))
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, position=1, content="new", created_at=_at(20))

    context = service.retrieve_context(
        _learner(),
        query="q",
        query_embedding=_emb(1.0),
        filters=RetrievalFilters(date_from=_at(10)),
    )
    assert [item.content for item in context.items] == ["new"]


def test_retrieve_context_excludes_non_live_lifecycle_rows() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    deleted_chunk_source = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, deleted_chunk_source, content="soft-deleted", deleted_at=_at(2))
    deleted_source = _make_source(engine, LOCAL_LEARNER_ID, None, status="deleted", deleted_at=_at(2))
    _make_chunk(engine, LOCAL_LEARNER_ID, deleted_source, content="dead-source")
    live_source = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, live_source, content="live")

    context = service.retrieve_context(_learner(), query="q", query_embedding=_emb(1.0))
    assert [item.content for item in context.items] == ["live"]


def test_retrieve_context_ranks_canonical_ahead_of_chunks() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, content="chunk", embedding=_emb(1.0, 0.0))
    canonical = make_item(
        kind=EvidenceKind.CANONICAL,
        ref_id="assignment-1",
        content="Assignment due Friday.",
        relevance=0.1,
        recorded_at=_at(1),
    )

    context = service.retrieve_context(
        _learner(), query="q", query_embedding=_emb(1.0, 0.0), canonical=[canonical]
    )
    assert context.items[0].kind is EvidenceKind.CANONICAL


def test_retrieve_context_degrades_when_graph_unavailable() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock(), graph_retrieval=UnavailableGraphRetrieval())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, content="chunk")

    context = service.retrieve_context(_learner(), query="q", query_embedding=_emb(1.0))
    assert context.status.graph_available is False
    assert "graph_unavailable" in context.status.degraded_reasons
    # Vector evidence is still present and not masqueraded as unavailable.
    assert context.status.vector_available is True
    assert not context.is_empty


def test_retrieve_context_includes_permitted_graph_facts() -> None:
    engine = _seeded_engine()

    class FakeGraph(UnavailableGraphRetrieval):
        def __init__(self) -> None:
            self.groups: list[str] = []

        def search_facts(self, *, owner_user_id, groups, query, limit):
            self.groups = list(groups)
            return [
                make_item(
                    kind=EvidenceKind.GRAPH_FACT,
                    ref_id="fact-1",
                    content="Derived fact.",
                    relevance=0.8,
                    recorded_at=_at(1),
                    graph_related=True,
                    user_confidence=0.9,
                )
            ]

    graph = FakeGraph()
    service = _service(engine, _clock(), graph_retrieval=graph)
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, content="chunk", embedding=_emb(1.0))

    context = service.retrieve_context(_learner(), query="q", query_embedding=_emb(1.0))
    kinds = {item.kind for item in context.items}
    assert EvidenceKind.GRAPH_FACT in kinds
    assert context.status.graph_available is True
    assert graph.groups == [f"user:{LOCAL_LEARNER_ID}"]


def test_retrieve_context_restricts_graph_groups_to_permitted_subjects() -> None:
    engine = _seeded_engine()
    subject_id = _make_subject(engine, LOCAL_LEARNER_ID, "math")

    class RecordingGraph(UnavailableGraphRetrieval):
        def __init__(self) -> None:
            self.groups: list[str] = []

        def search_facts(self, *, owner_user_id, groups, query, limit):
            self.groups = list(groups)
            return []

    graph = RecordingGraph()
    service = _service(engine, _clock(), graph_retrieval=graph)

    service.retrieve_context(
        _learner(),
        query="q",
        query_embedding=_emb(1.0),
        filters=RetrievalFilters(permitted_subject_ids=frozenset({subject_id})),
    )
    assert graph.groups == [f"user:{LOCAL_LEARNER_ID}:subject:{subject_id}"]


def test_retrieve_context_enforces_record_limit() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock(), retrieval_limits=RetrievalLimits(max_records=2, max_source_chunks=8))
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    for position in range(5):
        _make_chunk(engine, LOCAL_LEARNER_ID, source_id, position=position, content=f"chunk-{position}")

    context = service.retrieve_context(_learner(), query="q", query_embedding=_emb(1.0))
    assert len(context.items) == 2
    assert context.dropped_records == 3


def test_retrieve_context_writes_retrieval_log_with_ids_only() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    source_id = _make_source(engine, LOCAL_LEARNER_ID, None)
    _make_chunk(engine, LOCAL_LEARNER_ID, source_id, content="chunk")

    service.retrieve_context(_learner(), query="secret note contents", query_embedding=_emb(1.0))
    with engine.connect() as connection:
        row = connection.execute(select(memory_retrieval_log)).mappings().one()
        assert row["result_count"] == 1
        assert "secret note contents" not in str(row["result_ids"])
        assert "secret note contents" != row["query_hash"]


def test_retrieve_context_requires_memory_read_scope() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    with pytest.raises(AuthorizationError):
        service.retrieve_context(_observer(frozenset()), query="q", query_embedding=_emb(1.0))


def test_observer_with_memory_read_scope_may_retrieve() -> None:
    engine = _seeded_engine()
    service = _service(engine, _clock())
    context = service.retrieve_context(
        _observer(frozenset({"memory:read"})), query="q", query_embedding=_emb(1.0)
    )
    assert context.is_empty  # no chunks, but the read is authorized
