"""Grounded retrieval domain: bounded, deduplicated, deterministic ranking.

This module holds the pure value objects and deterministic rules that assemble a
``Retrieval_Context`` from three ordered evidence tiers:

1. ``Canonical_State`` (authoritative assignments, notes, curriculum) is always
   retrieved and ranked before supplementary evidence (Requirement 10.2).
2. Owner-filtered source chunks retrieved from pgvector *after* the owner,
   permitted-subject, date, and live-lifecycle filters have been applied by the
   repository (Requirements 9.7, 10.3).
3. Permitted Graph_Memory facts restricted to owner/subject groups
   (Requirement 10.4).

The context is deduplicated and ranked by source relevance, recency, graph
relationship, subject match, and user confidence (Requirement 10.5), and bounded
by configured record and token limits before any AI request (Requirements 10.6,
22.4). Ranking uses versioned fixed-point integer arithmetic so equal inputs
always produce equal output with no floating-point nondeterminism.

The module contains no persistence, framework, or provider dependencies so the
rules stay independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from math import sqrt
from typing import Any, Protocol, Sequence
from uuid import UUID


#: Versioned deterministic ranking rule identifier recorded with every result.
RANKING_RULE_VERSION = "retrieval-rank-v1"

#: Fixed-point scale for deterministic integer ranking arithmetic.
_SCALE = 1000


class EvidenceKind(StrEnum):
    """The evidence tier a retrieval item belongs to."""

    CANONICAL = "canonical"
    SOURCE_CHUNK = "source_chunk"
    GRAPH_FACT = "graph_fact"


#: Canonical evidence is always ordered ahead of supplementary evidence so an
#: answer is grounded in authoritative state first (Requirement 10.2).
_EVIDENCE_TIER: dict[EvidenceKind, int] = {
    EvidenceKind.CANONICAL: 0,
    EvidenceKind.SOURCE_CHUNK: 1,
    EvidenceKind.GRAPH_FACT: 2,
}


# ---------------------------------------------------------------------------
# Configuration value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalFilters:
    """Exact, non-broadening filters applied before similarity ranking.

    ``permitted_subject_ids`` of ``None`` means the query is not subject
    restricted (all of the authenticated owner's subjects are permitted). An
    explicit (possibly empty) set restricts retrieval to exactly those
    subjects; an empty set therefore yields an empty result rather than
    broadening scope (Requirements 9.7, 9.8).
    """

    permitted_subject_ids: frozenset[UUID] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    focus_subject_id: UUID | None = None


@dataclass(frozen=True)
class RetrievalLimits:
    """Configured record and token bounds enforced before an AI request."""

    max_records: int = 12
    max_source_chunks: int = 8
    max_graph_facts: int = 6
    max_chars_per_item: int = 2000
    max_total_tokens: int = 6000
    #: Candidate rows fetched per tier before ranking/bounding.
    candidate_limit: int = 40

    def __post_init__(self) -> None:
        for name in (
            "max_records",
            "max_source_chunks",
            "max_graph_facts",
            "max_chars_per_item",
            "max_total_tokens",
            "candidate_limit",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class RankingWeights:
    """Versioned deterministic weights for the composite ranking score."""

    relevance: int = 1000
    recency: int = 300
    graph: int = 150
    subject: int = 200
    confidence: int = 150
    version: str = RANKING_RULE_VERSION


# ---------------------------------------------------------------------------
# Evidence items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalItem:
    """A single unit of authorized evidence considered for the context."""

    kind: EvidenceKind
    ref_id: str
    dedup_key: str
    content: str
    relevance: float
    recorded_at: datetime
    subject_id: UUID | None = None
    subject_match: bool = False
    graph_related: bool = False
    user_confidence: float | None = None
    token_estimate: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def tier(self) -> int:
        return _EVIDENCE_TIER[self.kind]


@dataclass(frozen=True)
class RetrievalStatus:
    """Availability of each supplementary retrieval path (Requirement 10.10)."""

    vector_available: bool = True
    graph_available: bool = True
    degraded_reasons: tuple[str, ...] = ()

    @property
    def supplementary_available(self) -> bool:
        return self.vector_available and self.graph_available

    @property
    def is_degraded(self) -> bool:
        return not self.supplementary_available

    def as_dict(self) -> dict[str, Any]:
        return {
            "vector_available": self.vector_available,
            "graph_available": self.graph_available,
            "supplementary_available": self.supplementary_available,
            "degraded": self.is_degraded,
            "degraded_reasons": list(self.degraded_reasons),
        }


@dataclass(frozen=True)
class RetrievalContext:
    """The bounded, ranked context handed to response generation."""

    items: tuple[RetrievalItem, ...]
    status: RetrievalStatus
    rule_version: str
    total_tokens: int
    dropped_records: int

    @property
    def is_empty(self) -> bool:
        """True when authorized retrieval produced no supporting records.

        Callers state that no supporting learner records were found rather than
        broadening the authorized scope (Requirement 10.9).
        """
        return len(self.items) == 0


# ---------------------------------------------------------------------------
# Supplementary graph retrieval port (implemented by the Graphiti adapter)
# ---------------------------------------------------------------------------


class SupplementaryUnavailableError(RuntimeError):
    """A supplementary retrieval path (vector or graph) is unavailable.

    Raising this signals degraded supplementary context; it never broadens the
    authorized scope or fabricates grounded evidence (Requirement 10.10).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GraphRetrievalPort(Protocol):
    """Port for permitted Graph_Memory retrieval used by the memory service.

    Implementations restrict retrieval to the exact ``group`` values derived
    from the authenticated owner/subject scope (Requirement 10.4) and raise
    :class:`SupplementaryUnavailableError` when the graph store is unavailable.
    """

    def search_facts(
        self,
        *,
        owner_user_id: UUID,
        groups: Sequence[str],
        query: str,
        limit: int,
    ) -> Sequence[RetrievalItem]: ...


class UnavailableGraphRetrieval(GraphRetrievalPort):
    """Default graph retrieval that reports the graph store as unavailable.

    Used for local degraded operation until the Graphiti adapter is wired in;
    every call surfaces a typed supplementary-context outage so the assistant
    avoids presenting unsupported claims as grounded.
    """

    def search_facts(
        self,
        *,
        owner_user_id: UUID,
        groups: Sequence[str],
        query: str,
        limit: int,
    ) -> Sequence[RetrievalItem]:
        raise SupplementaryUnavailableError("graph_unavailable")


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Deterministic conservative token estimate for budget enforcement."""
    if not text:
        return 0
    # Roughly four characters per token, always at least one token for content.
    return max(1, (len(text) + 3) // 4)


def clamp_unit(value: float | None) -> float:
    """Clamp an optional score into the inclusive range 0..1."""
    if value is None:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors, mapped into 0..1.

    A zero-magnitude vector yields ``0.0``. The raw cosine in ``[-1, 1]`` is
    mapped to ``[0, 1]`` so it can serve directly as a relevance score.
    """
    if a is None or b is None:
        return 0.0
    length = min(len(a), len(b))
    if length == 0:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for index in range(length):
        av = float(a[index])
        bv = float(b[index])
        dot += av * bv
        norm_a += av * av
        norm_b += bv * bv
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    cosine = dot / (sqrt(norm_a) * sqrt(norm_b))
    return clamp_unit((cosine + 1.0) / 2.0)


def make_item(
    *,
    kind: EvidenceKind,
    ref_id: str,
    content: str,
    relevance: float,
    recorded_at: datetime,
    dedup_key: str | None = None,
    subject_id: UUID | None = None,
    subject_match: bool = False,
    graph_related: bool = False,
    user_confidence: float | None = None,
    provenance: dict[str, Any] | None = None,
) -> RetrievalItem:
    """Build a :class:`RetrievalItem`, computing its token estimate."""
    return RetrievalItem(
        kind=kind,
        ref_id=ref_id,
        dedup_key=dedup_key or f"{kind.value}:{ref_id}",
        content=content,
        relevance=clamp_unit(relevance),
        recorded_at=_as_utc(recorded_at),
        subject_id=subject_id,
        subject_match=subject_match,
        graph_related=graph_related,
        user_confidence=None if user_confidence is None else clamp_unit(user_confidence),
        token_estimate=estimate_tokens(content),
        provenance=dict(provenance or {}),
    )


def deduplicate(items: Sequence[RetrievalItem]) -> list[RetrievalItem]:
    """Collapse items sharing a ``dedup_key`` to a single best representative.

    The retained item is the one with the highest relevance, breaking ties by
    most recent capture and then by a stable reference id, so deduplication is
    deterministic and order-independent (Requirement 10.5).
    """
    best: dict[str, RetrievalItem] = {}
    for item in items:
        current = best.get(item.dedup_key)
        if current is None or _dedup_preference(item) < _dedup_preference(current):
            best[item.dedup_key] = item
    return list(best.values())


def rank(
    items: Sequence[RetrievalItem], weights: RankingWeights = RankingWeights()
) -> list[RetrievalItem]:
    """Deterministically order evidence by tier then composite score.

    Canonical evidence sorts ahead of supplementary evidence; within a tier,
    items sort by descending composite score with a stable reference-id
    tie-break (Requirements 10.2, 10.5).
    """
    if not items:
        return []
    recency = _recency_norms(items)
    scored = [
        (
            item.tier,
            -_score(item, recency[item.dedup_key], weights),
            item.dedup_key,
            item,
        )
        for item in items
    ]
    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in scored]


def bound(
    items: Sequence[RetrievalItem], limits: RetrievalLimits
) -> tuple[list[RetrievalItem], int, int]:
    """Enforce per-kind, record, character, and token limits deterministically.

    Returns the bounded items, the total estimated tokens, and the number of
    dropped records. Content is truncated to the per-item character budget and
    its token estimate recomputed before the total-token budget is applied
    (Requirements 10.6, 22.4).
    """
    per_kind_cap = {
        EvidenceKind.SOURCE_CHUNK: limits.max_source_chunks,
        EvidenceKind.GRAPH_FACT: limits.max_graph_facts,
    }
    selected: list[RetrievalItem] = []
    per_kind_count: dict[EvidenceKind, int] = {kind: 0 for kind in EvidenceKind}
    total_tokens = 0
    dropped = 0
    for item in items:
        if len(selected) >= limits.max_records:
            dropped += 1
            continue
        cap = per_kind_cap.get(item.kind)
        if cap is not None and per_kind_count[item.kind] >= cap:
            dropped += 1
            continue
        trimmed = _truncate(item, limits.max_chars_per_item)
        if total_tokens + trimmed.token_estimate > limits.max_total_tokens:
            dropped += 1
            continue
        selected.append(trimmed)
        per_kind_count[item.kind] += 1
        total_tokens += trimmed.token_estimate
    return selected, total_tokens, dropped


def assemble_context(
    *,
    canonical: Sequence[RetrievalItem] = (),
    source_chunks: Sequence[RetrievalItem] = (),
    graph_facts: Sequence[RetrievalItem] = (),
    limits: RetrievalLimits = RetrievalLimits(),
    weights: RankingWeights = RankingWeights(),
    vector_available: bool = True,
    graph_available: bool = True,
) -> RetrievalContext:
    """Combine the three evidence tiers into a bounded, ranked context.

    The steps are: gather all authorized evidence, deduplicate by canonical
    identity, rank deterministically with canonical evidence first, then bound
    by the configured record and token limits. Supplementary availability is
    reported so a degraded retrieval never masquerades as grounded
    (Requirements 10.2, 10.5, 10.6, 10.9, 10.10, 22.4).
    """
    combined: list[RetrievalItem] = [*canonical, *source_chunks, *graph_facts]
    deduped = deduplicate(combined)
    ranked = rank(deduped, weights)
    items, total_tokens, dropped = bound(ranked, limits)

    reasons: list[str] = []
    if not vector_available:
        reasons.append("vector_unavailable")
    if not graph_available:
        reasons.append("graph_unavailable")
    status = RetrievalStatus(
        vector_available=vector_available,
        graph_available=graph_available,
        degraded_reasons=tuple(reasons),
    )
    return RetrievalContext(
        items=tuple(items),
        status=status,
        rule_version=weights.version,
        total_tokens=total_tokens,
        dropped_records=dropped,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedup_preference(item: RetrievalItem) -> tuple[float, float, str]:
    """Lower is preferred: highest relevance, then most recent, then stable id."""
    return (-item.relevance, -item.recorded_at.timestamp(), item.ref_id)


def _recency_norms(items: Sequence[RetrievalItem]) -> dict[str, float]:
    """Normalize capture times into 0..1 across the candidate set."""
    stamps = [item.recorded_at.timestamp() for item in items]
    low = min(stamps)
    high = max(stamps)
    span = high - low
    norms: dict[str, float] = {}
    for item in items:
        if span <= 0.0:
            norms[item.dedup_key] = 1.0
        else:
            norms[item.dedup_key] = (item.recorded_at.timestamp() - low) / span
    return norms


def _score(item: RetrievalItem, recency_norm: float, weights: RankingWeights) -> int:
    """Composite fixed-point integer score for deterministic ordering."""
    relevance = int(round(clamp_unit(item.relevance) * _SCALE))
    recency = int(round(clamp_unit(recency_norm) * _SCALE))
    confidence = int(round(clamp_unit(item.user_confidence) * _SCALE))
    graph = _SCALE if item.graph_related else 0
    subject = _SCALE if item.subject_match else 0
    return (
        weights.relevance * relevance
        + weights.recency * recency
        + weights.graph * graph
        + weights.subject * subject
        + weights.confidence * confidence
    )


def _truncate(item: RetrievalItem, max_chars: int) -> RetrievalItem:
    if max_chars <= 0 or len(item.content) <= max_chars:
        return item
    trimmed = item.content[:max_chars]
    return replace(item, content=trimmed, token_estimate=estimate_tokens(trimmed))
