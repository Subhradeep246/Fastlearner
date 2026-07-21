"""Graph memory domain model: scopes, episodes, facts, and the vendor-neutral port.

This module holds the pure value objects, the :class:`GraphMemoryPort` protocol,
deterministic text chunking, and the provenance-verification rule for
graph-derived facts. It contains no persistence, framework, provider, or graph
SDK dependencies so the rules stay independently testable.

Design intent (Requirement 9 and 16):

* Graph groups are derived **exactly** from the authenticated owner scope as
  ``user:{owner_id}`` or ``user:{owner_id}:subject:{subject_id}`` (Requirement
  9.4). A client-supplied identifier never influences the group.
* Every ingested episode carries metadata that lets a derived fact cite its
  supporting episode, source, subject, visibility, creation time, confidence,
  and schema version (Requirement 9.5).
* A returned fact is rejected unless its provenance maps to a live, permitted
  canonical episode inside the requested scope (Requirement 9.5).
* ``Canonical_State`` is authoritative; graph facts never override canonical
  assignments, mastery, curriculum, schedules, permissions, or lifecycle
  (Requirements 9.6 and 16.13). That precedence rule lives in
  :mod:`app.domain.memory` and is reused here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol, Sequence
from uuid import UUID

#: The graph metadata schema version. Bumping it lets the adapter and later
#: migrations reason about facts produced by an older extraction schema.
GRAPH_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class GraphError(RuntimeError):
    """Base class for typed, safe-to-surface graph-memory errors."""

    code = "graph_error"
    retryable = False

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class GraphUnavailableError(GraphError):
    """The graph store is unavailable or a graph operation failed transiently.

    The error is retryable so a durable ingestion or retraction job stays
    eligible for retry while the accepted canonical episode is retained
    (Requirements 9.10, 16.4, 20.6).
    """

    code = "graph_unavailable"
    retryable = True


# ---------------------------------------------------------------------------
# Scopes, episodes, and facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphScope:
    """The owner- and optionally subject-scoped group for a graph operation."""

    owner_id: UUID
    subject_id: UUID | None = None

    def group(self) -> str:
        """Return the exact Graphiti group for this scope (Requirement 9.4)."""
        if self.subject_id is None:
            return f"user:{self.owner_id}"
        return f"user:{self.owner_id}:subject:{self.subject_id}"


@dataclass(frozen=True)
class GraphEpisode:
    """An accepted canonical episode offered for graph ingestion.

    ``content`` is the deliberately saved episode text. The remaining fields are
    provenance metadata that a derived fact can cite back to the supporting
    canonical episode (Requirement 9.5).
    """

    episode_id: UUID
    source_id: UUID
    owner_id: UUID
    group: str
    content: str
    subject_id: UUID | None = None
    visibility: str = "private"
    confidence: float | None = None
    created_at: datetime | None = None
    schema_version: str = GRAPH_SCHEMA_VERSION

    def metadata(self) -> dict[str, Any]:
        """JSON-serializable provenance metadata attached to the graph episode."""
        created = self.created_at
        created_iso = (
            created.astimezone(timezone.utc).isoformat() if created is not None else None
        )
        return {
            "episode_id": str(self.episode_id),
            "source_id": str(self.source_id),
            "owner_id": str(self.owner_id),
            "group": self.group,
            "subject_id": str(self.subject_id) if self.subject_id is not None else None,
            "visibility": self.visibility,
            "confidence": self.confidence,
            "created_at": created_iso,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class GraphFact:
    """A graph-derived fact returned from a scoped search.

    ``episode_id`` and ``source_id`` reference the supporting canonical records
    so provenance can be verified before the fact is used (Requirement 9.5).
    """

    fact_id: str
    episode_id: UUID
    source_id: UUID
    group: str
    statement: str
    subject_id: UUID | None = None
    visibility: str = "private"
    confidence: float | None = None
    created_at: datetime | None = None
    schema_version: str = GRAPH_SCHEMA_VERSION


@dataclass(frozen=True)
class GraphReceipt:
    """The outcome of an ingest or retract operation."""

    episode_id: UUID
    group: str
    status: str  # "ingested" | "retracted" | "skipped"
    fact_count: int = 0


@dataclass(frozen=True)
class DependencyHealth:
    """The health of the graph dependency for degraded-operation reporting."""

    available: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Vendor-neutral graph memory port
# ---------------------------------------------------------------------------


class GraphMemoryPort(Protocol):
    """Vendor-neutral port for subject/owner-scoped temporal graph memory.

    Implemented by :class:`~app.adapters.graph.GraphitiGraphMemory` for the
    Neo4j-backed Graphiti store and by
    :class:`~app.adapters.graph.UnavailableGraphMemory` for local degraded
    operation.
    """

    async def ingest_episode(self, episode: GraphEpisode) -> GraphReceipt: ...

    async def search(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]: ...

    async def retract_episode(self, scope: GraphScope, episode_id: UUID) -> GraphReceipt: ...

    async def health(self) -> DependencyHealth: ...


# ---------------------------------------------------------------------------
# Provenance verification (Requirement 9.5)
# ---------------------------------------------------------------------------


def verify_fact_provenance(
    scope: GraphScope,
    facts: Iterable[GraphFact],
    live_episode_ids: Iterable[UUID],
) -> list[GraphFact]:
    """Keep only facts whose provenance maps to a live, permitted episode.

    A fact survives verification only when its group matches the requested scope
    group (permitted owner and subject) and its supporting episode is in the set
    of live canonical episode identifiers. Facts referencing a deleted, retracted,
    or out-of-scope episode are dropped so a stale or foreign fact can never be
    presented as grounded (Requirement 9.5).
    """
    group = scope.group()
    live = set(live_episode_ids)
    return [fact for fact in facts if fact.group == group and fact.episode_id in live]


# ---------------------------------------------------------------------------
# Deterministic text chunking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkingConfig:
    """Bounded, deterministic chunking parameters for source ingestion."""

    max_characters: int = 1000
    overlap_characters: int = 100

    def __post_init__(self) -> None:
        if self.max_characters <= 0:
            raise ValueError("max_characters must be positive")
        if self.overlap_characters < 0:
            raise ValueError("overlap_characters must be non-negative")
        if self.overlap_characters >= self.max_characters:
            raise ValueError("overlap_characters must be smaller than max_characters")


@dataclass(frozen=True)
class TextChunk:
    """A single positioned chunk of source content."""

    position: int
    content: str


def chunk_text(content: str, config: ChunkingConfig | None = None) -> list[TextChunk]:
    """Split content into bounded, overlapping, position-ordered chunks.

    The function is total and deterministic: the same input always yields the
    same chunks. Empty or whitespace-only content yields no chunks. Consecutive
    chunks advance by ``max_characters - overlap_characters`` so context is
    preserved across chunk boundaries without unbounded growth.
    """
    settings = config or ChunkingConfig()
    text = (content or "").strip()
    if not text:
        return []

    stride = settings.max_characters - settings.overlap_characters
    chunks: list[TextChunk] = []
    position = 0
    start = 0
    length = len(text)
    while start < length:
        window = text[start : start + settings.max_characters]
        chunks.append(TextChunk(position=position, content=window))
        if start + settings.max_characters >= length:
            break
        position += 1
        start += stride
    return chunks


def graph_episode_from_metadata(
    *,
    owner_id: UUID,
    episode_id: UUID,
    source_id: UUID,
    content: str,
    group: str,
    subject_id: UUID | None,
    visibility: str,
    confidence: float | None,
    created_at: datetime | None,
) -> GraphEpisode:
    """Assemble a :class:`GraphEpisode` from an outbox payload and episode content."""
    return GraphEpisode(
        episode_id=episode_id,
        source_id=source_id,
        owner_id=owner_id,
        group=group,
        content=content,
        subject_id=subject_id,
        visibility=visibility,
        confidence=confidence,
        created_at=created_at,
    )


def parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from an outbox payload, tolerating ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_uuid(value: Any) -> UUID | None:
    """Parse a UUID from an outbox payload, tolerating ``None`` and empties."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def facts_within_scope(scope: GraphScope, facts: Sequence[GraphFact]) -> list[GraphFact]:
    """Keep only facts whose group matches the scope group (permitted owner/subject)."""
    group = scope.group()
    return [fact for fact in facts if fact.group == group]
