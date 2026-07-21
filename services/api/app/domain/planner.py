"""Deterministic study-planning domain rules.

This module holds the pure value objects, versioned scoring, stable
tie-breaking, availability clipping, and non-overlapping block allocation for
the Planner_Service (Requirement 8). It contains no persistence, framework, or
provider dependencies so the rules stay independently testable and, crucially,
deterministic: equal inputs always produce equal output.

Design constraints encoded here (see design "Planner" section):

* Candidate work is scored with a versioned integer/fixed-point rule so there is
  no floating-point nondeterminism (Requirements 8.1, 8.2).
* Equal candidate scores are resolved with a versioned deterministic tie-break
  ``(-score, due_at_or_max, kind_priority, stable_resource_uuid)``
  (Requirement 8.3).
* Availability is clipped against existing blocks; work is split into
  non-overlapping 15-to-45-minute study blocks placed chronologically within
  free windows, up to the requested and daily workload limits
  (Requirements 8.4, 8.8).
* Every produced block carries the exact scoring inputs, the selected score,
  constraint effects, source IDs, the rule version, and a human-readable reason
  (Requirement 8.5).
* Work that cannot be scheduled is reported with the limiting availability or
  workload constraint (Requirements 8.6, 8.11).
* Study-block lifecycle changes preserve the original reason history
  (Requirement 8.7).

The scoring inputs (mastery, fatigue, repetition, goal linkage, blocking) are
computed by the application service from prior canonical state and passed in as
immutable :class:`CandidateWork` values; scoring therefore never depends on the
allocation order, which keeps the whole pipeline referentially transparent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from app.domain.identity import IdentityError, ValidationError

__all__ = [
    "PLANNER_RULE_VERSION",
    "MIN_BLOCK_MINUTES",
    "MAX_BLOCK_MINUTES",
    "WorkKind",
    "StudyBlockStatus",
    "StudyBlockAction",
    "UnscheduledConstraint",
    "ScoreWeights",
    "DEFAULT_WEIGHTS",
    "CandidateWork",
    "ScoreBreakdown",
    "ScoredCandidate",
    "TimeInterval",
    "PlannedBlock",
    "UnscheduledWork",
    "PlanResult",
    "ReasonRecord",
    "InvalidStudyBlockTransitionError",
    "score_candidate",
    "score_candidates",
    "sort_candidates",
    "clip_availability",
    "allocate_plan",
    "build_reason",
    "next_study_block_status",
    "append_reason_history",
]


# ---------------------------------------------------------------------------
# Versioned constants
# ---------------------------------------------------------------------------

#: Version stamped onto every score breakdown and persisted reason so a plan can
#: be reproduced and audited against the exact rule that produced it.
PLANNER_RULE_VERSION = "planner-v1"

#: Study blocks are always between 15 and 45 minutes (Requirement 8.4).
MIN_BLOCK_MINUTES = 15
MAX_BLOCK_MINUTES = 45

#: Deadline urgency decays linearly across a one-week horizon; overdue work
#: earns a bounded extra bonus so it always outranks not-yet-due work.
_DEADLINE_HORIZON_MINUTES = 7 * 24 * 60
_REVIEW_HORIZON_MINUTES = 14 * 24 * 60

#: Mastery is expressed in basis points (0..10000) to avoid floating point; the
#: gap is the distance below full mastery.
_MASTERY_MAX_BP = 10_000


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class WorkKind(StrEnum):
    """The candidate work sources the planner balances (Requirement 8.1)."""

    OVERDUE_TASK = "overdue_task"
    NEAR_DEADLINE_TASK = "near_deadline_task"
    DUE_REVIEW = "due_review"
    BLOCKING_CONCEPT = "blocking_concept"
    GOAL = "goal"


#: Deterministic priority applied as a tie-break after score and due time. Lower
#: sorts earlier. Overdue and blocking work is preferred on ties.
_KIND_PRIORITY: dict[WorkKind, int] = {
    WorkKind.OVERDUE_TASK: 0,
    WorkKind.BLOCKING_CONCEPT: 1,
    WorkKind.DUE_REVIEW: 2,
    WorkKind.NEAR_DEADLINE_TASK: 3,
    WorkKind.GOAL: 4,
}


class StudyBlockStatus(StrEnum):
    """Persisted study-block lifecycle state (Requirement 8.7)."""

    PLANNED = "planned"
    ACTIVE = "active"
    SKIPPED = "skipped"
    DONE = "done"


class StudyBlockAction(StrEnum):
    """A learner-driven study-block lifecycle action (Requirement 8.7)."""

    EDIT = "edit"
    RESCHEDULE = "reschedule"
    START = "start"
    SKIP = "skip"
    COMPLETE = "complete"


class UnscheduledConstraint(StrEnum):
    """The limiting reason a candidate could not be fully scheduled."""

    #: No remaining availability window can hold a 15-minute block.
    NO_AVAILABILITY = "no_availability"
    #: The requested plan length was reached before this work fit.
    REQUESTED_LIMIT = "requested_limit"
    #: The configured maximum daily workload was reached.
    DAILY_LIMIT = "daily_limit"


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class InvalidStudyBlockTransitionError(IdentityError):
    """A study-block lifecycle transition is not permitted from the current state."""

    code = "invalid_study_block_transition"

    def __init__(self, current: StudyBlockStatus, action: StudyBlockAction) -> None:
        super().__init__(f"Cannot {action.value} a study block in '{current.value}' state.")
        self.current = current
        self.action = action


# ---------------------------------------------------------------------------
# Scoring weights (versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreWeights:
    """Versioned integer weights for the deterministic score rule.

    ``score = deadline_urgency + review_urgency + mastery_gap + goal_value``
    ``        - fatigue_penalty - repetition_penalty`` (Requirement 8.2).

    All arithmetic is integer-only so results are exactly reproducible.
    """

    version: str = PLANNER_RULE_VERSION
    deadline_weight: int = 5_000
    overdue_bonus_cap: int = 5_000
    review_weight: int = 4_000
    review_overdue_bonus_cap: int = 4_000
    mastery_weight: int = 4_000
    blocking_weight: int = 800
    goal_weight: int = 1_000
    fatigue_weight: int = 50
    repetition_weight: int = 600


DEFAULT_WEIGHTS = ScoreWeights()


# ---------------------------------------------------------------------------
# Candidate work and scoring value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateWork:
    """An immutable candidate unit of work derived from canonical state.

    The application service computes the raw signals (mastery, blocking count,
    goal linkage, fatigue, repetition) from prior canonical state so scoring is a
    pure function of these inputs (Requirement 8.1).
    """

    resource_id: UUID
    kind: WorkKind
    subject_id: UUID
    estimated_minutes: int
    title: str = ""
    due_at: datetime | None = None
    review_due_at: datetime | None = None
    #: Mastery probability in basis points (0..10000); ``None`` when not a
    #: mastery-driven candidate. The mastery gap is ``10000 - mastery_bp``.
    mastery_bp: int | None = None
    #: Count of successor concepts blocked by this prerequisite concept.
    blocks_concepts: int = 0
    #: Relative goal-linkage strength (0 when unrelated to an active goal).
    goal_linkage: int = 0
    #: Prior cumulative same-day load signal contributing fatigue penalty.
    fatigue_load: int = 0
    #: Count of recent blocks for the same subject/kind contributing repetition.
    repetition_recent: int = 0
    #: Canonical source IDs recorded in the persisted reason (Requirement 8.5).
    source_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ScoreBreakdown:
    """The exact, versioned scoring inputs and the selected score.

    Persisted verbatim into the study-block reason so a plan is fully explainable
    and reproducible (Requirement 8.5).
    """

    rule_version: str
    deadline_urgency: int
    review_urgency: int
    mastery_gap: int
    goal_value: int
    fatigue_penalty: int
    repetition_penalty: int
    score: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "deadline_urgency": self.deadline_urgency,
            "review_urgency": self.review_urgency,
            "mastery_gap": self.mastery_gap,
            "goal_value": self.goal_value,
            "fatigue_penalty": self.fatigue_penalty,
            "repetition_penalty": self.repetition_penalty,
            "score": self.score,
        }


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate paired with its deterministic score breakdown."""

    candidate: CandidateWork
    breakdown: ScoreBreakdown

    @property
    def score(self) -> int:
        return self.breakdown.score


@dataclass(frozen=True)
class TimeInterval:
    """A half-open ``[start, end)`` interval of schedulable time."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class PlannedBlock:
    """A scheduled 15-to-45-minute study block with its complete reason."""

    resource_id: UUID
    kind: WorkKind
    subject_id: UUID
    start: datetime
    end: datetime
    reason_json: Mapping[str, Any]
    reason_text: str

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class UnscheduledWork:
    """Work that could not be scheduled, with the limiting constraint."""

    resource_id: UUID
    kind: WorkKind
    subject_id: UUID
    remaining_minutes: int
    constraint: UnscheduledConstraint
    explanation: str


@dataclass(frozen=True)
class PlanResult:
    """The deterministic outcome of a plan request."""

    blocks: tuple[PlannedBlock, ...]
    unscheduled: tuple[UnscheduledWork, ...]
    rule_version: str = PLANNER_RULE_VERSION

    @property
    def scheduled_minutes(self) -> int:
        return sum(block.minutes for block in self.blocks)


@dataclass(frozen=True)
class ReasonRecord:
    """One immutable entry in a study block's reason history (Requirement 8.7)."""

    reason_json: Mapping[str, Any]
    reason_text: str
    recorded_at: datetime
    origin: str = "plan"


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------


def _whole_minutes_until(reference: datetime, target: datetime) -> int:
    """Signed whole minutes from ``reference`` to ``target`` (negative if past)."""
    return int((target - reference).total_seconds() // 60)


def _deadline_urgency(candidate: CandidateWork, now: datetime, weights: ScoreWeights) -> int:
    """Linear-decay deadline urgency with a bounded overdue bonus (integer)."""
    if candidate.due_at is None:
        return 0
    delta = _whole_minutes_until(now, candidate.due_at)
    if delta <= 0:
        overdue = -delta
        bonus = min(
            overdue * weights.deadline_weight // _DEADLINE_HORIZON_MINUTES,
            weights.overdue_bonus_cap,
        )
        return weights.deadline_weight + bonus
    remaining = max(0, _DEADLINE_HORIZON_MINUTES - delta)
    return remaining * weights.deadline_weight // _DEADLINE_HORIZON_MINUTES


def _review_urgency(candidate: CandidateWork, now: datetime, weights: ScoreWeights) -> int:
    """Linear-decay review urgency with a bounded overdue bonus (integer)."""
    if candidate.review_due_at is None:
        return 0
    delta = _whole_minutes_until(now, candidate.review_due_at)
    if delta <= 0:
        overdue = -delta
        bonus = min(
            overdue * weights.review_weight // _REVIEW_HORIZON_MINUTES,
            weights.review_overdue_bonus_cap,
        )
        return weights.review_weight + bonus
    remaining = max(0, _REVIEW_HORIZON_MINUTES - delta)
    return remaining * weights.review_weight // _REVIEW_HORIZON_MINUTES


def _mastery_gap(candidate: CandidateWork, weights: ScoreWeights) -> int:
    """Distance below full mastery plus a bonus for prerequisite blocking."""
    gap = 0
    if candidate.mastery_bp is not None:
        clamped = max(0, min(_MASTERY_MAX_BP, candidate.mastery_bp))
        gap = (_MASTERY_MAX_BP - clamped) * weights.mastery_weight // _MASTERY_MAX_BP
    return gap + max(0, candidate.blocks_concepts) * weights.blocking_weight


def score_candidate(
    candidate: CandidateWork,
    now: datetime,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> ScoreBreakdown:
    """Compute the versioned integer score breakdown for one candidate.

    ``score = deadline_urgency + review_urgency + mastery_gap + goal_value``
    ``        - fatigue_penalty - repetition_penalty`` (Requirement 8.2). All
    arithmetic is integer-only so equal inputs always yield equal output.
    """
    deadline_urgency = _deadline_urgency(candidate, now, weights)
    review_urgency = _review_urgency(candidate, now, weights)
    mastery_gap = _mastery_gap(candidate, weights)
    goal_value = max(0, candidate.goal_linkage) * weights.goal_weight
    fatigue_penalty = max(0, candidate.fatigue_load) * weights.fatigue_weight
    repetition_penalty = max(0, candidate.repetition_recent) * weights.repetition_weight
    score = (
        deadline_urgency
        + review_urgency
        + mastery_gap
        + goal_value
        - fatigue_penalty
        - repetition_penalty
    )
    return ScoreBreakdown(
        rule_version=weights.version,
        deadline_urgency=deadline_urgency,
        review_urgency=review_urgency,
        mastery_gap=mastery_gap,
        goal_value=goal_value,
        fatigue_penalty=fatigue_penalty,
        repetition_penalty=repetition_penalty,
        score=score,
    )


def score_candidates(
    candidates: Iterable[CandidateWork],
    now: datetime,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> tuple[ScoredCandidate, ...]:
    """Score every candidate, preserving input order."""
    return tuple(
        ScoredCandidate(candidate=candidate, breakdown=score_candidate(candidate, now, weights))
        for candidate in candidates
    )


# ---------------------------------------------------------------------------
# Deterministic tie-breaking
# ---------------------------------------------------------------------------

#: Sentinel used when a candidate has no due/review date so undated work always
#: sorts after dated work of equal score.
_MAX_EPOCH = 1 << 62


def _due_sort_key(candidate: CandidateWork) -> int:
    """Effective due time as integer epoch seconds, or a max sentinel."""
    effective = candidate.due_at or candidate.review_due_at
    if effective is None:
        return _MAX_EPOCH
    return int(effective.timestamp())


def sort_candidates(scored: Iterable[ScoredCandidate]) -> tuple[ScoredCandidate, ...]:
    """Order candidates by the versioned deterministic tie-break rule.

    ``sort key = (-score, due_at_or_max, kind_priority, stable_resource_uuid)``
    (Requirement 8.3). The final ``resource_id`` component guarantees a total,
    stable order so equal inputs always yield the same plan.
    """
    return tuple(
        sorted(
            scored,
            key=lambda item: (
                -item.breakdown.score,
                _due_sort_key(item.candidate),
                _KIND_PRIORITY[item.candidate.kind],
                str(item.candidate.resource_id),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Availability clipping
# ---------------------------------------------------------------------------


def clip_availability(
    windows: Iterable[TimeInterval],
    existing_blocks: Iterable[TimeInterval],
) -> tuple[TimeInterval, ...]:
    """Subtract existing blocks from availability windows.

    Returns the free intervals sorted chronologically, with overlapping or
    adjacent free spans merged. Existing blocks are treated as unavailable so new
    blocks never overlap committed schedule (Requirements 8.4, 8.9).
    """
    normalized = _merge_intervals(windows)
    busy = _merge_intervals(existing_blocks)

    free: list[TimeInterval] = []
    for window in normalized:
        cursor = window.start
        for block in busy:
            if block.end <= cursor or block.start >= window.end:
                continue
            if block.start > cursor:
                free.append(TimeInterval(cursor, min(block.start, window.end)))
            cursor = max(cursor, block.end)
            if cursor >= window.end:
                break
        if cursor < window.end:
            free.append(TimeInterval(cursor, window.end))
    return tuple(interval for interval in free if interval.minutes > 0)


def _merge_intervals(intervals: Iterable[TimeInterval]) -> tuple[TimeInterval, ...]:
    """Sort and merge overlapping/adjacent intervals into a minimal set."""
    ordered = sorted(
        (interval for interval in intervals if interval.end > interval.start),
        key=lambda interval: (interval.start, interval.end),
    )
    merged: list[TimeInterval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = TimeInterval(merged[-1].start, interval.end)
        else:
            merged.append(interval)
    return tuple(merged)


# ---------------------------------------------------------------------------
# Block-size chunking
# ---------------------------------------------------------------------------


def _chunk_minutes(total: int) -> tuple[tuple[int, ...], int]:
    """Split ``total`` minutes into valid 15-to-45-minute chunks.

    Returns the ordered chunk sizes and any leftover minutes below the minimum
    block size. Chunks are chosen so no leftover falls in the invalid 1-14 range
    between chunks: whenever removing a full 45-minute block would leave 1-14
    minutes, a smaller chunk is taken so the trailing block is exactly the
    minimum size.
    """
    chunks: list[int] = []
    remaining = total
    while remaining >= MIN_BLOCK_MINUTES:
        if remaining <= MAX_BLOCK_MINUTES:
            chunks.append(remaining)
            remaining = 0
        elif remaining - MAX_BLOCK_MINUTES < MIN_BLOCK_MINUTES:
            chunk = remaining - MIN_BLOCK_MINUTES
            chunks.append(chunk)
            remaining -= chunk
        else:
            chunks.append(MAX_BLOCK_MINUTES)
            remaining -= MAX_BLOCK_MINUTES
    return tuple(chunks), remaining


# ---------------------------------------------------------------------------
# Greedy non-overlapping allocation
# ---------------------------------------------------------------------------


@dataclass
class _FreeCursor:
    """Mutable view over remaining free intervals during allocation."""

    intervals: list[TimeInterval]

    def place(self, minutes: int) -> TimeInterval | None:
        """Reserve ``minutes`` from the earliest interval that can hold them."""
        for index, interval in enumerate(self.intervals):
            if interval.minutes >= minutes:
                start = interval.start
                end = _add_minutes(start, minutes)
                remainder = TimeInterval(end, interval.end)
                if remainder.minutes > 0:
                    self.intervals[index] = remainder
                else:
                    del self.intervals[index]
                return TimeInterval(start, end)
        return None

    def extend_last(self, block: TimeInterval, minutes: int) -> TimeInterval | None:
        """Extend ``block`` by ``minutes`` when the following time is still free."""
        for index, interval in enumerate(self.intervals):
            if interval.start == block.end and interval.minutes >= minutes:
                new_end = _add_minutes(block.end, minutes)
                remainder = TimeInterval(new_end, interval.end)
                if remainder.minutes > 0:
                    self.intervals[index] = remainder
                else:
                    del self.intervals[index]
                return TimeInterval(block.start, new_end)
        return None

    @property
    def has_capacity(self) -> bool:
        return any(interval.minutes >= MIN_BLOCK_MINUTES for interval in self.intervals)


def _add_minutes(moment: datetime, minutes: int) -> datetime:
    return moment + timedelta(minutes=minutes)


def allocate_plan(
    scored: Sequence[ScoredCandidate],
    free_intervals: Iterable[TimeInterval],
    *,
    now: datetime,
    requested_minutes: int,
    daily_limit_minutes: int,
    daily_used_minutes: int = 0,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> PlanResult:
    """Greedily place scored work into non-overlapping 15-45 minute blocks.

    Candidates are placed in the pre-sorted order; each is split into valid
    chunks and placed chronologically in the earliest free interval that fits,
    without overlap (Requirements 8.4, 8.8). Placement stops at the smaller of
    the requested plan length and the remaining daily workload budget
    (Requirements 8.1, 8.6). A trailing remainder below the minimum block size is
    merged into the candidate's last block only when the result stays within the
    maximum block size; otherwise it is reported unscheduled (Requirement 8.6).
    When no window can hold even a minimum block, no new blocks are produced and
    the unavailable capacity is reported (Requirement 8.11).

    The result is deterministic: identical inputs always produce identical
    blocks, reasons, and unscheduled explanations.
    """
    if requested_minutes < 0 or daily_limit_minutes < 0 or daily_used_minutes < 0:
        raise ValidationError("Plan minute budgets must be non-negative.", field="requested_minutes")

    remaining_daily = max(0, daily_limit_minutes - daily_used_minutes)
    budget = min(requested_minutes, remaining_daily)
    cursor = _FreeCursor(intervals=list(free_intervals))

    blocks: list[PlannedBlock] = []
    unscheduled: list[UnscheduledWork] = []

    for item in scored:
        candidate = item.candidate
        target_minutes = max(0, candidate.estimated_minutes)
        if target_minutes == 0:
            continue

        # Clamp to the remaining budget before chunking.
        limited_by_budget = target_minutes > budget
        schedulable = min(target_minutes, budget)

        placed_minutes = 0
        last_block: TimeInterval | None = None

        if schedulable >= MIN_BLOCK_MINUTES and cursor.intervals:
            chunk_sizes, _leftover = _chunk_minutes(schedulable)
            for chunk in chunk_sizes:
                if budget - placed_minutes < chunk:
                    break
                slot = cursor.place(chunk)
                if slot is None:
                    break
                block = _make_block(candidate, item.breakdown, slot, now, weights)
                blocks.append(block)
                placed_minutes += chunk
                last_block = slot

        budget -= placed_minutes
        remaining_minutes = target_minutes - placed_minutes

        # Attempt to merge a sub-minimum remainder into the last block.
        if (
            last_block is not None
            and 0 < remaining_minutes < MIN_BLOCK_MINUTES
            and last_block.minutes + remaining_minutes <= MAX_BLOCK_MINUTES
            and budget >= remaining_minutes
        ):
            extended = cursor.extend_last(last_block, remaining_minutes)
            if extended is not None:
                blocks[-1] = _make_block(candidate, item.breakdown, extended, now, weights)
                budget -= remaining_minutes
                remaining_minutes = target_minutes - extended.minutes

        if remaining_minutes > 0:
            constraint = _classify_constraint(
                placed_minutes=placed_minutes,
                remaining_minutes=remaining_minutes,
                budget=budget,
                limited_by_budget=limited_by_budget,
                has_capacity=cursor.has_capacity,
                remaining_daily=remaining_daily,
                requested_minutes=requested_minutes,
            )
            unscheduled.append(
                UnscheduledWork(
                    resource_id=candidate.resource_id,
                    kind=candidate.kind,
                    subject_id=candidate.subject_id,
                    remaining_minutes=remaining_minutes,
                    constraint=constraint,
                    explanation=_constraint_explanation(constraint, remaining_minutes, candidate),
                )
            )

    return PlanResult(blocks=tuple(blocks), unscheduled=tuple(unscheduled))


def _classify_constraint(
    *,
    placed_minutes: int,
    remaining_minutes: int,
    budget: int,
    limited_by_budget: bool,
    has_capacity: bool,
    remaining_daily: int,
    requested_minutes: int,
) -> UnscheduledConstraint:
    """Determine the limiting constraint for unscheduled work."""
    if budget < MIN_BLOCK_MINUTES and (limited_by_budget or placed_minutes > 0):
        # The plan budget was exhausted; attribute to the tighter of the two.
        if remaining_daily <= requested_minutes:
            return UnscheduledConstraint.DAILY_LIMIT
        return UnscheduledConstraint.REQUESTED_LIMIT
    if not has_capacity:
        return UnscheduledConstraint.NO_AVAILABILITY
    # Capacity exists but only in fragments smaller than the remaining need.
    return UnscheduledConstraint.NO_AVAILABILITY


def _constraint_explanation(
    constraint: UnscheduledConstraint, remaining_minutes: int, candidate: CandidateWork
) -> str:
    label = candidate.title.strip() or candidate.kind.value.replace("_", " ")
    if constraint is UnscheduledConstraint.NO_AVAILABILITY:
        return (
            f"'{label}' has {remaining_minutes} minute(s) unscheduled because no "
            "available window can hold a study block."
        )
    if constraint is UnscheduledConstraint.DAILY_LIMIT:
        return (
            f"'{label}' has {remaining_minutes} minute(s) unscheduled because the "
            "maximum daily workload was reached."
        )
    return (
        f"'{label}' has {remaining_minutes} minute(s) unscheduled because the "
        "requested plan length was reached."
    )


# ---------------------------------------------------------------------------
# Reason construction (Requirement 8.5)
# ---------------------------------------------------------------------------


def build_reason(
    candidate: CandidateWork,
    breakdown: ScoreBreakdown,
    slot: TimeInterval,
    *,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> tuple[dict[str, Any], str]:
    """Build the persisted ``reason_json`` and human-readable reason text.

    The reason captures the rule version, all score inputs, the selected score,
    constraint effects, and source IDs so a study block is fully explainable and
    reproducible (Requirement 8.5).
    """
    reason_json: dict[str, Any] = {
        "rule_version": weights.version,
        "kind": candidate.kind.value,
        "subject_id": str(candidate.subject_id),
        "resource_id": str(candidate.resource_id),
        "allocated_minutes": slot.minutes,
        "score_inputs": {
            "estimated_minutes": candidate.estimated_minutes,
            "due_at": candidate.due_at.isoformat() if candidate.due_at else None,
            "review_due_at": candidate.review_due_at.isoformat()
            if candidate.review_due_at
            else None,
            "mastery_bp": candidate.mastery_bp,
            "blocks_concepts": candidate.blocks_concepts,
            "goal_linkage": candidate.goal_linkage,
            "fatigue_load": candidate.fatigue_load,
            "repetition_recent": candidate.repetition_recent,
        },
        "score_breakdown": breakdown.as_dict(),
        "selected_score": breakdown.score,
        "source_ids": [str(source_id) for source_id in candidate.source_ids],
    }
    label = candidate.title.strip() or candidate.kind.value.replace("_", " ")
    reason_text = (
        f"Scheduled {slot.minutes} minute(s) for '{label}' "
        f"({candidate.kind.value.replace('_', ' ')}) with priority score "
        f"{breakdown.score} under rule {weights.version}."
    )
    return reason_json, reason_text


def _make_block(
    candidate: CandidateWork,
    breakdown: ScoreBreakdown,
    slot: TimeInterval,
    now: datetime,
    weights: ScoreWeights,
) -> PlannedBlock:
    reason_json, reason_text = build_reason(candidate, breakdown, slot, weights=weights)
    return PlannedBlock(
        resource_id=candidate.resource_id,
        kind=candidate.kind,
        subject_id=candidate.subject_id,
        start=slot.start,
        end=slot.end,
        reason_json=reason_json,
        reason_text=reason_text,
    )


# ---------------------------------------------------------------------------
# Study-block lifecycle and reason-history preservation (Requirement 8.7)
# ---------------------------------------------------------------------------


#: Allowed study-block lifecycle transitions per learner action.
_STUDY_BLOCK_TRANSITIONS: dict[
    StudyBlockAction, tuple[frozenset[StudyBlockStatus], StudyBlockStatus]
] = {
    # Editing or rescheduling keeps a block in the planned state.
    StudyBlockAction.EDIT: (
        frozenset({StudyBlockStatus.PLANNED}),
        StudyBlockStatus.PLANNED,
    ),
    StudyBlockAction.RESCHEDULE: (
        frozenset({StudyBlockStatus.PLANNED, StudyBlockStatus.SKIPPED}),
        StudyBlockStatus.PLANNED,
    ),
    StudyBlockAction.START: (
        frozenset({StudyBlockStatus.PLANNED}),
        StudyBlockStatus.ACTIVE,
    ),
    StudyBlockAction.SKIP: (
        frozenset({StudyBlockStatus.PLANNED, StudyBlockStatus.ACTIVE}),
        StudyBlockStatus.SKIPPED,
    ),
    StudyBlockAction.COMPLETE: (
        frozenset({StudyBlockStatus.PLANNED, StudyBlockStatus.ACTIVE}),
        StudyBlockStatus.DONE,
    ),
}


def next_study_block_status(
    current: StudyBlockStatus, action: StudyBlockAction
) -> StudyBlockStatus:
    """Return the resulting status for a study-block ``action`` or raise.

    Encodes the planned/active/skipped/done lifecycle (Requirement 8.7). Invalid
    transitions raise :class:`InvalidStudyBlockTransitionError` and never change
    canonical state.
    """
    allowed_from, target = _STUDY_BLOCK_TRANSITIONS[action]
    if current not in allowed_from:
        raise InvalidStudyBlockTransitionError(current, action)
    return target


def append_reason_history(
    history: Sequence[ReasonRecord],
    new_record: ReasonRecord,
) -> tuple[ReasonRecord, ...]:
    """Append a new reason record while preserving the original history.

    Lifecycle changes (edit, reschedule, skip, start, complete) never overwrite
    the original planning reason; each change adds a new immutable record so the
    full reason history is retained (Requirement 8.7).
    """
    return tuple(history) + (new_record,)
