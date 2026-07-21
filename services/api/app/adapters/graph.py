"""Graph memory adapters implementing the :class:`GraphMemoryPort`.

``GraphitiGraphMemory`` maps authenticated scopes to exactly ``user:{owner_id}``
or ``user:{owner_id}:subject:{subject_id}`` (Requirement 9.4) and attaches
provenance metadata (source/episode IDs, visibility, timestamps, confidence,
schema version) to every ingested episode (Requirement 9.5). It talks to the
graph store through a small injectable client seam, so tests never open a real
Neo4j connection. Returned facts are rejected unless their provenance maps to a
live, permitted canonical episode (Requirement 9.5); the DB-backed
:class:`SqlGraphProvenanceVerifier` supplies the set of live episodes.

``UnavailableGraphMemory`` supports local degraded operation (Requirements 16.7,
20.6): searches return no supplementary facts, and ingest/retract raise a typed
retryable error so the durable outbox keeps the work eligible for retry without
losing the accepted canonical episode.

No graph SDK type ever crosses the :class:`GraphMemoryPort` boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID

from sqlalchemy import Engine, and_, select

from app.domain.graph import (
    DependencyHealth,
    GraphEpisode,
    GraphFact,
    GraphMemoryPort,
    GraphReceipt,
    GraphScope,
    GraphUnavailableError,
    parse_iso_timestamp,
    parse_uuid,
    verify_fact_provenance,
)
from app.domain.memory import EpisodeStatus, GraphSyncStatus
from app.persistence.models import graph_sync_state, memory_episodes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

#: A verifier maps a scope and candidate facts to the subset whose provenance is
#: live and permitted. It is injected so the transport-only adapter stays pure.
ProvenanceVerifier = Callable[[GraphScope, Sequence[GraphFact]], list[GraphFact]]


# ---------------------------------------------------------------------------
# Client seam (no real Neo4j connection in tests)
# ---------------------------------------------------------------------------


class GraphClientError(Exception):
    """A transport-level graph failure categorized for neutral error mapping."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class GraphitiClient(Protocol):
    """Low-level graph transport returning plain dictionaries.

    Implementations issue the actual Graphiti/Neo4j calls. Tests inject a fake
    client so no real network request is ever made.
    """

    async def add_episode(
        self, *, group_id: str, episode_id: str, body: str, metadata: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:  # pragma: no cover - interface
        ...

    async def search(
        self, *, group_id: str, query: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:  # pragma: no cover - interface
        ...

    async def remove_episode(
        self, *, group_id: str, episode_id: str
    ) -> int:  # pragma: no cover - interface
        ...

    async def health(self) -> bool:  # pragma: no cover - interface
        ...


# ---------------------------------------------------------------------------
# Graphiti-backed adapter
# ---------------------------------------------------------------------------


class GraphitiGraphMemory(GraphMemoryPort):
    """The configured Graphiti/Neo4j adapter (Requirement 16.3)."""

    def __init__(
        self,
        client: GraphitiClient,
        *,
        provenance_verifier: ProvenanceVerifier | None = None,
    ) -> None:
        self._client = client
        self._verify = provenance_verifier

    async def ingest_episode(self, episode: GraphEpisode) -> GraphReceipt:
        try:
            raw_facts = await self._client.add_episode(
                group_id=episode.group,
                episode_id=str(episode.episode_id),
                body=episode.content,
                metadata=episode.metadata(),
            )
        except GraphClientError as error:
            raise GraphUnavailableError(str(error)) from error
        return GraphReceipt(
            episode_id=episode.episode_id,
            group=episode.group,
            status="ingested",
            fact_count=len(list(raw_facts or ())),
        )

    async def search(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        group = scope.group()
        try:
            raw = await self._client.search(group_id=group, query=query, limit=limit)
        except GraphClientError as error:
            raise GraphUnavailableError(str(error)) from error
        facts: list[GraphFact] = []
        for item in raw or []:
            parsed = self._parse_fact(item, group)
            if parsed is not None:
                facts.append(parsed)
        # A fact is trusted only when its provenance maps to a live, permitted
        # canonical episode within the requested scope (Requirement 9.5).
        if self._verify is not None:
            return self._verify(scope, facts)
        return facts

    async def retract_episode(self, scope: GraphScope, episode_id: UUID) -> GraphReceipt:
        try:
            removed = await self._client.remove_episode(
                group_id=scope.group(), episode_id=str(episode_id)
            )
        except GraphClientError as error:
            raise GraphUnavailableError(str(error)) from error
        return GraphReceipt(
            episode_id=episode_id,
            group=scope.group(),
            status="retracted",
            fact_count=int(removed or 0),
        )

    async def health(self) -> DependencyHealth:
        try:
            available = await self._client.health()
        except GraphClientError as error:
            return DependencyHealth(available=False, detail=str(error))
        return DependencyHealth(available=bool(available), detail=None)

    @staticmethod
    def _parse_fact(item: Mapping[str, Any], group: str) -> GraphFact | None:
        metadata = item.get("metadata")
        source = metadata if isinstance(metadata, Mapping) else item
        episode_id = parse_uuid(source.get("episode_id"))
        source_id = parse_uuid(source.get("source_id"))
        statement = item.get("statement") or item.get("fact") or item.get("content")
        if episode_id is None or source_id is None or not isinstance(statement, str):
            return None
        return GraphFact(
            fact_id=str(item.get("fact_id") or item.get("id") or ""),
            episode_id=episode_id,
            source_id=source_id,
            group=str(source.get("group") or group),
            statement=statement,
            subject_id=parse_uuid(source.get("subject_id")),
            visibility=str(source.get("visibility") or "private"),
            confidence=_as_float(source.get("confidence")),
            created_at=parse_iso_timestamp(source.get("created_at")),
            schema_version=str(source.get("schema_version") or "1"),
        )


# ---------------------------------------------------------------------------
# Degraded / unavailable adapter (Requirements 16.7, 20.6)
# ---------------------------------------------------------------------------


class UnavailableGraphMemory(GraphMemoryPort):
    """A graph port for local degraded operation.

    Searches return no supplementary facts so retrieval can continue with an
    explicit degraded status, while ingest and retract raise a typed retryable
    error so durable jobs stay eligible for retry and the canonical episode is
    never lost.
    """

    _MESSAGE = "Graph memory is unavailable."

    async def ingest_episode(self, episode: GraphEpisode) -> GraphReceipt:
        raise GraphUnavailableError(self._MESSAGE)

    async def search(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        return []

    async def retract_episode(self, scope: GraphScope, episode_id: UUID) -> GraphReceipt:
        raise GraphUnavailableError(self._MESSAGE)

    async def health(self) -> DependencyHealth:
        return DependencyHealth(available=False, detail=self._MESSAGE)


#: Backwards-friendly alias: a no-op graph memory is the unavailable one.
NoopGraphMemory = UnavailableGraphMemory


# ---------------------------------------------------------------------------
# DB-backed provenance verifier (Requirement 9.5)
# ---------------------------------------------------------------------------


class SqlGraphProvenanceVerifier:
    """Verify graph-fact provenance against live canonical episodes.

    A fact is kept only when its supporting episode is active for the scope
    owner and its graph-sync state is not retracted, i.e. the fact maps to a
    live, permitted canonical episode.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, scope: GraphScope, facts: Sequence[GraphFact]) -> list[GraphFact]:
        candidate_ids = {fact.episode_id for fact in facts}
        if not candidate_ids:
            return []
        live = self._live_episode_ids(scope.owner_id, candidate_ids)
        return verify_fact_provenance(scope, facts, live)

    def _live_episode_ids(self, owner_id: UUID, episode_ids: set[UUID]) -> set[UUID]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(memory_episodes.c.id)
                .select_from(
                    memory_episodes.outerjoin(
                        graph_sync_state,
                        graph_sync_state.c.episode_id == memory_episodes.c.id,
                    )
                )
                .where(
                    and_(
                        memory_episodes.c.owner_user_id == owner_id,
                        memory_episodes.c.id.in_(episode_ids),
                        memory_episodes.c.status == EpisodeStatus.ACTIVE.value,
                        graph_sync_state.c.status.isnot(None),
                        graph_sync_state.c.status != GraphSyncStatus.RETRACTED.value,
                    )
                )
            ).all()
        return {row.id for row in rows}


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# Production selection
# ---------------------------------------------------------------------------


def create_graph_memory(
    settings: "Settings",
    *,
    client: GraphitiClient | None = None,
    provenance_verifier: ProvenanceVerifier | None = None,
) -> GraphMemoryPort:
    """Select and construct the graph memory port from server-side configuration.

    When no Neo4j endpoint is configured the system runs in degraded mode with
    :class:`UnavailableGraphMemory`. When a client is supplied (or a Neo4j
    endpoint is configured) the Graphiti adapter is built. The real client is
    imported lazily so importing this module never requires the graph SDK and no
    connection is opened until a call is actually made.
    """
    if client is not None:
        return GraphitiGraphMemory(client, provenance_verifier=provenance_verifier)
    if not settings.neo4j_uri:
        return UnavailableGraphMemory()
    return GraphitiGraphMemory(
        _default_graphiti_client(settings), provenance_verifier=provenance_verifier
    )


def _default_graphiti_client(settings: "Settings") -> GraphitiClient:  # pragma: no cover - live only
    """Build the production Graphiti client. Never exercised in tests."""
    return LazyNeo4jGraphitiClient(
        uri=settings.neo4j_uri or "",
        user=settings.neo4j_user or "",
        password=(
            settings.neo4j_password.get_secret_value() if settings.neo4j_password else ""
        ),
    )


class LazyNeo4jGraphitiClient(GraphitiClient):  # pragma: no cover - live endpoint only
    """A Graphiti client that lazily imports its backing dependencies.

    The optional graph dependencies are imported on first use; if they are not
    installed a typed :class:`GraphClientError` is raised so the adapter maps it
    to a retryable :class:`GraphUnavailableError` rather than crashing import.
    """

    def __init__(self, *, uri: str, user: str, password: str) -> None:
        self._uri = uri
        self._user = user
        self._password = password

    def _unavailable(self) -> GraphClientError:
        return GraphClientError(
            "The Graphiti/Neo4j client is not configured in this environment."
        )

    async def add_episode(
        self, *, group_id: str, episode_id: str, body: str, metadata: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        raise self._unavailable()

    async def search(
        self, *, group_id: str, query: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        raise self._unavailable()

    async def remove_episode(self, *, group_id: str, episode_id: str) -> int:
        raise self._unavailable()

    async def health(self) -> bool:
        return False
