"""Curriculum graph, reviewed-content lifecycle, and practice-serving rules.

This module holds the pure value objects, enumerations, transition rules, and
typed domain errors for the Curriculum_Service (Requirement 12). It contains no
persistence, framework, or provider dependencies so the rules stay
independently testable.

Key rules encoded here:

* Every prerequisite graph is validated as acyclic before publication and, on
  failure, the exact involved concept edges are reported (Requirements 12.2,
  12.23).
* Lesson, question, hint, and explanation content moves through the
  draft -> reviewed -> published -> retired lifecycle (Requirement 12.20).
* Generated content lacking an approving review is restricted to draft/review
  contexts and cannot be published (Requirements 12.21, 12.22).
* Retired versions are excluded from new practice while remaining referenceable
  for historical attempts (Requirement 12.24).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping
from uuid import UUID

from app.domain.identity import IdentityError, ValidationError

__all__ = [
    "ContentAction",
    "ContentKind",
    "ContentState",
    "ContentItem",
    "ConceptEdge",
    "CyclicPrerequisiteError",
    "ReviewDecision",
    "ReviewerDecision",
    "QuestionVersion",
    "next_content_state",
    "record_reviewer_decision",
    "select_servable_versions",
    "topological_order",
    "validate_acyclic",
]


# ---------------------------------------------------------------------------
# Lifecycle enumerations (the four-state content lifecycle of Requirement 12.20)
# ---------------------------------------------------------------------------


class ContentKind(StrEnum):
    """Reviewed content kinds served for a concept."""

    LESSON = "lesson"
    HINT = "hint"
    EXPLANATION = "explanation"


class ContentState(StrEnum):
    """Content lifecycle states (Requirement 12.20)."""

    DRAFT = "draft"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    RETIRED = "retired"


class ContentAction(StrEnum):
    """A lifecycle action applied to a content item or question version."""

    APPROVE = "approve"
    PUBLISH = "publish"
    RETIRE = "retire"


class ReviewDecision(StrEnum):
    """An adult or administrator review decision (Requirement 12.22)."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class CyclicPrerequisiteError(IdentityError):
    """Prerequisite validation detected a cycle (Requirement 12.23).

    The error carries the exact ordered concept edges that form the cycle so a
    caller can identify and repair the involved prerequisite relationships.
    """

    code = "curriculum_cycle_detected"

    def __init__(self, cycle_edges: tuple["ConceptEdge", ...]) -> None:
        self.cycle_edges = cycle_edges
        rendered = ", ".join(f"{edge.concept_id}<-{edge.prerequisite_concept_id}" for edge in cycle_edges)
        super().__init__(f"Prerequisite graph contains a cycle: {rendered}")

    def safe_payload(self) -> dict[str, Any]:
        payload = super().safe_payload()
        payload["cycle_edges"] = [
            {"concept_id": str(edge.concept_id), "prerequisite_concept_id": str(edge.prerequisite_concept_id)}
            for edge in self.cycle_edges
        ]
        return payload


class InvalidContentTransitionError(IdentityError):
    """A content lifecycle transition is not permitted from the current state."""

    code = "invalid_content_transition"

    def __init__(self, current: ContentState, action: ContentAction) -> None:
        super().__init__(f"Cannot {action.value} content in '{current.value}' state.")
        self.current = current
        self.action = action


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptEdge:
    """A prerequisite edge: ``prerequisite_concept_id`` must precede ``concept_id``."""

    concept_id: UUID
    prerequisite_concept_id: UUID


@dataclass(frozen=True)
class ContentItem:
    """An immutable versioned content item (Requirement 12.1)."""

    id: UUID
    concept_id: UUID
    kind: ContentKind
    version: int
    state: ContentState
    title: str
    body: str
    checksum: str


@dataclass(frozen=True)
class QuestionVersion:
    """An immutable versioned question (Requirement 12.1)."""

    id: UUID
    concept_id: UUID
    question_key: str
    version: int
    state: ContentState
    prompt: str
    answer_spec: Mapping[str, Any]
    explanation: str
    provenance: Mapping[str, Any]
    checksum: str


@dataclass(frozen=True)
class ReviewerDecision:
    """A recorded reviewer decision made before publication (Requirement 12.22)."""

    reviewer_user_id: UUID
    decision: ReviewDecision
    version: int
    reviewed_at: datetime
    source: str
    notes: str | None = None

    @property
    def approves_publication(self) -> bool:
        return self.decision is ReviewDecision.APPROVED


# ---------------------------------------------------------------------------
# Acyclic prerequisite-graph validation with cycle-edge reporting
# ---------------------------------------------------------------------------


def _adjacency(edges: Iterable[ConceptEdge]) -> dict[UUID, list[ConceptEdge]]:
    """Map each prerequisite to the edges that depend on it (prereq -> concept)."""
    successors: dict[UUID, list[ConceptEdge]] = {}
    for edge in edges:
        successors.setdefault(edge.prerequisite_concept_id, []).append(edge)
    return successors


def _find_cycle_edges(
    nodes: Iterable[UUID], successors: Mapping[UUID, list[ConceptEdge]]
) -> tuple[ConceptEdge, ...]:
    """Return the ordered edges of one prerequisite cycle, or an empty tuple.

    Uses an iterative depth-first search over the dependency edges
    (prerequisite -> dependent concept) and, on encountering a back edge to a
    node already on the active path, reconstructs the exact edges that close the
    cycle so publication failures can name the involved relationships.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[UUID, int] = {node: WHITE for node in nodes}
    # Each stack frame tracks the node and the edge that led into it.
    for root in list(color):
        if color[root] != WHITE:
            continue
        stack: list[tuple[UUID, list[ConceptEdge], ConceptEdge | None]] = [
            (root, list(successors.get(root, ())), None)
        ]
        path_edges: list[ConceptEdge] = []
        path_nodes: list[UUID] = [root]
        color[root] = GREY
        while stack:
            node, pending, _incoming = stack[-1]
            if pending:
                edge = pending.pop()
                target = edge.concept_id
                if color.get(target, BLACK) == GREY:
                    # Found a back edge: reconstruct the cycle from target.
                    start = path_nodes.index(target)
                    cycle = path_edges[start:] + [edge]
                    return tuple(cycle)
                if color.get(target, BLACK) == WHITE:
                    color[target] = GREY
                    path_nodes.append(target)
                    path_edges.append(edge)
                    stack.append((target, list(successors.get(target, ())), edge))
            else:
                color[node] = BLACK
                stack.pop()
                if path_nodes:
                    path_nodes.pop()
                if path_edges:
                    path_edges.pop()
    return ()


def topological_order(
    concept_ids: Iterable[UUID], edges: Iterable[ConceptEdge]
) -> tuple[UUID, ...]:
    """Return a prerequisite-first ordering of concepts or raise on a cycle.

    Prerequisites always appear before the concepts that depend on them. If the
    graph contains a cycle the exact involved edges are reported through
    :class:`CyclicPrerequisiteError` (Requirements 12.2, 12.23).
    """
    nodes = list(dict.fromkeys(concept_ids))
    node_set = set(nodes)
    edge_list = list(edges)
    for edge in edge_list:
        # Edges must reference known concepts; unknown references are a
        # validation error rather than a silent skip.
        if edge.concept_id not in node_set or edge.prerequisite_concept_id not in node_set:
            raise ValidationError(
                "Prerequisite edge references an unknown concept.",
                field="edges",
            )

    successors = _adjacency(edge_list)
    indegree: dict[UUID, int] = {node: 0 for node in nodes}
    for edge in edge_list:
        indegree[edge.concept_id] += 1

    # Kahn's algorithm; ties broken by the input concept order for determinism.
    ready = [node for node in nodes if indegree[node] == 0]
    ordered: list[UUID] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for edge in successors.get(node, ()):
            indegree[edge.concept_id] -= 1
            if indegree[edge.concept_id] == 0:
                ready.append(edge.concept_id)

    if len(ordered) != len(nodes):
        remaining = [node for node in nodes if node not in set(ordered)]
        cycle = _find_cycle_edges(remaining, successors)
        raise CyclicPrerequisiteError(cycle)
    return tuple(ordered)


def validate_acyclic(concept_ids: Iterable[UUID], edges: Iterable[ConceptEdge]) -> tuple[UUID, ...]:
    """Validate a Concept_DAG as acyclic before publication (Requirement 12.2)."""
    return topological_order(concept_ids, edges)


# ---------------------------------------------------------------------------
# Content lifecycle transitions and reviewer decisions
# ---------------------------------------------------------------------------


#: Allowed content lifecycle transitions (Requirement 12.20).
_CONTENT_TRANSITIONS: dict[ContentAction, tuple[frozenset[ContentState], ContentState]] = {
    ContentAction.APPROVE: (
        frozenset({ContentState.DRAFT, ContentState.REVIEWED}),
        ContentState.REVIEWED,
    ),
    ContentAction.PUBLISH: (
        frozenset({ContentState.REVIEWED}),
        ContentState.PUBLISHED,
    ),
    ContentAction.RETIRE: (
        frozenset({ContentState.PUBLISHED}),
        ContentState.RETIRED,
    ),
}


def next_content_state(
    current: ContentState,
    action: ContentAction,
    *,
    approving_review: ReviewerDecision | None = None,
) -> ContentState:
    """Return the state after applying ``action`` or raise a typed error.

    Publication requires a recorded approving review; without one the content is
    restricted to draft or review contexts (Requirements 12.21, 12.22). Invalid
    transitions raise :class:`InvalidContentTransitionError` and never change
    canonical state.
    """
    allowed_from, target = _CONTENT_TRANSITIONS[action]
    if current not in allowed_from:
        raise InvalidContentTransitionError(current, action)
    if action is ContentAction.PUBLISH and (
        approving_review is None or not approving_review.approves_publication
    ):
        raise InvalidContentTransitionError(current, action)
    return target


def record_reviewer_decision(
    *,
    reviewer_user_id: UUID | None,
    decision: ReviewDecision | str,
    version: int,
    reviewed_at: datetime,
    source: str,
    notes: str | None = None,
) -> ReviewerDecision:
    """Validate and build a reviewer decision recorded before publication.

    Captures reviewer, decision, notes, version, source, and decision time
    (Requirement 12.22). All fields are validated with typed errors so an
    invalid decision never advances the content lifecycle.
    """
    if reviewer_user_id is None:
        raise ValidationError("A reviewer is required to record a decision.", field="reviewer_user_id")
    try:
        resolved = ReviewDecision(decision)
    except ValueError as error:
        raise ValidationError("Unknown review decision.", field="decision") from error
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("Review version must be a positive integer.", field="version")
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValidationError("Review time must include a UTC offset.", field="reviewed_at")
    clean_source = (source or "").strip()
    if not clean_source:
        raise ValidationError("A review source is required.", field="source")
    return ReviewerDecision(
        reviewer_user_id=reviewer_user_id,
        decision=resolved,
        version=version,
        reviewed_at=reviewed_at,
        source=clean_source,
        notes=(notes.strip() if isinstance(notes, str) and notes.strip() else None),
    )


def is_servable(state: ContentState) -> bool:
    """Only published content is served for new practice (Requirement 12.24)."""
    return state is ContentState.PUBLISHED


def select_servable_versions(
    items: Iterable[ContentItem | QuestionVersion],
) -> tuple[ContentItem | QuestionVersion, ...]:
    """Filter to published versions, excluding retired and unreviewed content.

    Retired versions are withheld from new practice while remaining available by
    direct reference for historical attempts (Requirement 12.24).
    """
    return tuple(item for item in items if is_servable(item.state))
