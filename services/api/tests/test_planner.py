"""Unit tests for the pure deterministic planner domain rules.

Covers Requirement 8: versioned integer scoring (8.1, 8.2), stable tie-breaking
(8.3), availability clipping and 15-to-45-minute non-overlapping allocation
(8.4, 8.8), complete persisted reason inputs (8.5), unscheduled-work constraint
explanations (8.6, 8.11), and study-block lifecycle with preserved reason
history (8.7).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.domain.planner import (
    MAX_BLOCK_MINUTES,
    MIN_BLOCK_MINUTES,
    PLANNER_RULE_VERSION,
    CandidateWork,
    InvalidStudyBlockTransitionError,
    ReasonRecord,
    StudyBlockAction,
    StudyBlockStatus,
    TimeInterval,
    UnscheduledConstraint,
    WorkKind,
    allocate_plan,
    append_reason_history,
    build_reason,
    clip_availability,
    next_study_block_status,
    score_candidate,
    score_candidates,
    sort_candidates,
)

NOW = datetime(2025, 1, 6, 8, 0, tzinfo=timezone.utc)


def _uuid(tag: int) -> UUID:
    return UUID(int=tag)


def _candidate(tag: int, **overrides) -> CandidateWork:
    base = dict(
        resource_id=_uuid(tag),
        kind=WorkKind.NEAR_DEADLINE_TASK,
        subject_id=_uuid(1000 + tag),
        estimated_minutes=30,
    )
    base.update(overrides)
    return CandidateWork(**base)


def _window(start_hour: int, minutes: int) -> TimeInterval:
    start = NOW.replace(hour=start_hour, minute=0)
    return TimeInterval(start, start + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# Scoring (8.1, 8.2) — integer/fixed-point determinism
# ---------------------------------------------------------------------------


def test_score_is_integer_and_versioned() -> None:
    candidate = _candidate(1, due_at=NOW + timedelta(hours=6), mastery_bp=4000)
    breakdown = score_candidate(candidate, NOW)
    assert isinstance(breakdown.score, int)
    assert breakdown.rule_version == PLANNER_RULE_VERSION
    # score = deadline + review + mastery_gap + goal - fatigue - repetition
    assert breakdown.score == (
        breakdown.deadline_urgency
        + breakdown.review_urgency
        + breakdown.mastery_gap
        + breakdown.goal_value
        - breakdown.fatigue_penalty
        - breakdown.repetition_penalty
    )


def test_scoring_is_deterministic_for_equal_inputs() -> None:
    candidate = _candidate(2, due_at=NOW + timedelta(days=1), mastery_bp=2500, goal_linkage=3)
    first = score_candidate(candidate, NOW)
    second = score_candidate(candidate, NOW)
    assert first == second


def test_overdue_task_outranks_future_task() -> None:
    overdue = _candidate(3, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1))
    future = _candidate(4, due_at=NOW + timedelta(days=5))
    assert score_candidate(overdue, NOW).score > score_candidate(future, NOW).score


def test_lower_mastery_produces_larger_gap() -> None:
    weak = _candidate(5, kind=WorkKind.BLOCKING_CONCEPT, mastery_bp=1000)
    strong = _candidate(6, kind=WorkKind.BLOCKING_CONCEPT, mastery_bp=9000)
    assert score_candidate(weak, NOW).mastery_gap > score_candidate(strong, NOW).mastery_gap


def test_fatigue_and_repetition_reduce_score() -> None:
    base = _candidate(7, due_at=NOW + timedelta(hours=12))
    penalized = _candidate(
        7, due_at=NOW + timedelta(hours=12), fatigue_load=10, repetition_recent=3
    )
    assert score_candidate(penalized, NOW).score < score_candidate(base, NOW).score


# ---------------------------------------------------------------------------
# Tie-breaking (8.3)
# ---------------------------------------------------------------------------


def test_equal_scores_break_ties_deterministically_by_resource_uuid() -> None:
    # Two identical undated goal candidates with equal score: order by UUID.
    a = _candidate(20, kind=WorkKind.GOAL, estimated_minutes=30)
    b = _candidate(10, kind=WorkKind.GOAL, estimated_minutes=30)
    scored = score_candidates([a, b], NOW)
    ordered = sort_candidates(scored)
    assert [item.candidate.resource_id for item in ordered] == [_uuid(10), _uuid(20)]
    # Sorting is stable regardless of input order.
    reordered = sort_candidates(score_candidates([b, a], NOW))
    assert [item.candidate.resource_id for item in reordered] == [_uuid(10), _uuid(20)]


def test_higher_score_sorts_first() -> None:
    urgent = _candidate(30, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=2))
    relaxed = _candidate(31, due_at=NOW + timedelta(days=6))
    ordered = sort_candidates(score_candidates([relaxed, urgent], NOW))
    assert ordered[0].candidate.resource_id == _uuid(30)


# ---------------------------------------------------------------------------
# Availability clipping (8.4, 8.9)
# ---------------------------------------------------------------------------


def test_clip_availability_removes_existing_blocks() -> None:
    window = _window(9, 120)  # 09:00-11:00
    busy = TimeInterval(NOW.replace(hour=9, minute=30), NOW.replace(hour=10, minute=0))
    free = clip_availability([window], [busy])
    assert free == (
        TimeInterval(NOW.replace(hour=9, minute=0), NOW.replace(hour=9, minute=30)),
        TimeInterval(NOW.replace(hour=10, minute=0), NOW.replace(hour=11, minute=0)),
    )


def test_clip_availability_merges_adjacent_windows() -> None:
    first = _window(9, 60)
    second = _window(10, 60)  # contiguous with first
    free = clip_availability([first, second], [])
    assert free == (TimeInterval(NOW.replace(hour=9), NOW.replace(hour=11)),)


# ---------------------------------------------------------------------------
# Allocation: block bounds and non-overlap (8.4, 8.8)
# ---------------------------------------------------------------------------


def test_all_blocks_are_within_15_to_45_minutes_and_non_overlapping() -> None:
    free = clip_availability([_window(9, 180)], [])
    candidates = [
        _candidate(40, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=90),
        _candidate(41, due_at=NOW + timedelta(hours=2), estimated_minutes=50),
    ]
    scored = sort_candidates(score_candidates(candidates, NOW))
    result = allocate_plan(
        scored, free, now=NOW, requested_minutes=180, daily_limit_minutes=240
    )
    assert result.blocks, "expected scheduled blocks"
    for block in result.blocks:
        assert MIN_BLOCK_MINUTES <= block.minutes <= MAX_BLOCK_MINUTES
    ordered = sorted(result.blocks, key=lambda b: b.start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.end <= later.start


def test_ninety_minute_request_totals_at_most_ninety(
) -> None:
    # Requirement 8.8: 90-minute request with >= 90 available minutes.
    free = clip_availability([_window(9, 240)], [])
    candidates = [
        _candidate(50, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=60),
        _candidate(51, due_at=NOW + timedelta(hours=3), estimated_minutes=60),
    ]
    scored = sort_candidates(score_candidates(candidates, NOW))
    result = allocate_plan(
        scored, free, now=NOW, requested_minutes=90, daily_limit_minutes=240
    )
    assert result.scheduled_minutes <= 90
    ordered = sorted(result.blocks, key=lambda b: b.start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.end <= later.start


def test_allocation_is_deterministic() -> None:
    free = clip_availability([_window(9, 180)], [])
    candidates = [
        _candidate(60, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=45),
        _candidate(61, due_at=NOW + timedelta(hours=4), estimated_minutes=45),
    ]
    scored = sort_candidates(score_candidates(candidates, NOW))
    first = allocate_plan(scored, free, now=NOW, requested_minutes=120, daily_limit_minutes=240)
    scored2 = sort_candidates(score_candidates(candidates, NOW))
    free2 = clip_availability([_window(9, 180)], [])
    second = allocate_plan(scored2, free2, now=NOW, requested_minutes=120, daily_limit_minutes=240)
    assert [(b.start, b.end) for b in first.blocks] == [(b.start, b.end) for b in second.blocks]


# ---------------------------------------------------------------------------
# Reason persistence (8.5)
# ---------------------------------------------------------------------------


def test_reason_json_captures_all_score_inputs_and_sources() -> None:
    candidate = _candidate(
        70,
        kind=WorkKind.DUE_REVIEW,
        review_due_at=NOW - timedelta(hours=1),
        mastery_bp=3000,
        source_ids=(_uuid(700), _uuid(701)),
        estimated_minutes=30,
    )
    breakdown = score_candidate(candidate, NOW)
    slot = TimeInterval(NOW.replace(hour=9), NOW.replace(hour=9, minute=30))
    reason_json, reason_text = build_reason(candidate, breakdown, slot)
    assert reason_json["rule_version"] == PLANNER_RULE_VERSION
    assert reason_json["selected_score"] == breakdown.score
    assert reason_json["score_breakdown"] == breakdown.as_dict()
    assert reason_json["source_ids"] == [str(_uuid(700)), str(_uuid(701))]
    assert reason_json["allocated_minutes"] == 30
    assert set(reason_json["score_inputs"]) == {
        "estimated_minutes",
        "due_at",
        "review_due_at",
        "mastery_bp",
        "blocks_concepts",
        "goal_linkage",
        "fatigue_load",
        "repetition_recent",
    }
    assert str(breakdown.score) in reason_text


def test_scheduled_block_carries_reason() -> None:
    free = clip_availability([_window(9, 60)], [])
    candidate = _candidate(71, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=30)
    scored = sort_candidates(score_candidates([candidate], NOW))
    result = allocate_plan(scored, free, now=NOW, requested_minutes=60, daily_limit_minutes=120)
    assert result.blocks[0].reason_json["resource_id"] == str(_uuid(71))
    assert result.blocks[0].reason_text


# ---------------------------------------------------------------------------
# Unscheduled work and constraint explanations (8.6, 8.11)
# ---------------------------------------------------------------------------


def test_requested_limit_reports_unscheduled_work() -> None:
    free = clip_availability([_window(9, 240)], [])
    candidates = [
        _candidate(80, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=45),
        _candidate(81, due_at=NOW + timedelta(hours=2), estimated_minutes=45),
    ]
    scored = sort_candidates(score_candidates(candidates, NOW))
    result = allocate_plan(scored, free, now=NOW, requested_minutes=45, daily_limit_minutes=240)
    assert result.scheduled_minutes == 45
    assert result.unscheduled
    assert result.unscheduled[0].constraint in {
        UnscheduledConstraint.REQUESTED_LIMIT,
        UnscheduledConstraint.DAILY_LIMIT,
    }
    assert result.unscheduled[0].explanation


def test_no_window_yields_zero_blocks_and_reports_unavailable_capacity() -> None:
    # Requirement 8.11: no availability window can hold a study block.
    free: tuple[TimeInterval, ...] = ()
    candidate = _candidate(90, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=30)
    scored = sort_candidates(score_candidates([candidate], NOW))
    result = allocate_plan(scored, free, now=NOW, requested_minutes=60, daily_limit_minutes=120)
    assert result.blocks == ()
    assert result.unscheduled[0].constraint is UnscheduledConstraint.NO_AVAILABILITY
    assert "no available window" in result.unscheduled[0].explanation


def test_window_too_small_reports_no_availability() -> None:
    free = clip_availability([_window(9, 10)], [])  # only 10 minutes free
    candidate = _candidate(91, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=30)
    scored = sort_candidates(score_candidates([candidate], NOW))
    result = allocate_plan(scored, free, now=NOW, requested_minutes=60, daily_limit_minutes=120)
    assert result.blocks == ()
    assert result.unscheduled[0].constraint is UnscheduledConstraint.NO_AVAILABILITY


def test_daily_limit_caps_scheduling() -> None:
    free = clip_availability([_window(9, 240)], [])
    candidate = _candidate(92, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=90)
    scored = sort_candidates(score_candidates([candidate], NOW))
    result = allocate_plan(
        scored, free, now=NOW, requested_minutes=200, daily_limit_minutes=60, daily_used_minutes=30
    )
    # Only 30 minutes of daily budget remain.
    assert result.scheduled_minutes <= 30
    assert result.unscheduled
    assert result.unscheduled[0].constraint is UnscheduledConstraint.DAILY_LIMIT


# ---------------------------------------------------------------------------
# Sub-minimum remainder merge behaviour (8.4/8.6)
# ---------------------------------------------------------------------------


def test_small_remainder_merges_into_block_when_within_max() -> None:
    # 40 minutes chunks to a single 40 block (<=45), nothing unscheduled.
    free = clip_availability([_window(9, 60)], [])
    candidate = _candidate(100, kind=WorkKind.OVERDUE_TASK, due_at=NOW - timedelta(days=1), estimated_minutes=40)
    scored = sort_candidates(score_candidates([candidate], NOW))
    result = allocate_plan(scored, free, now=NOW, requested_minutes=60, daily_limit_minutes=120)
    assert result.scheduled_minutes == 40
    assert result.unscheduled == ()


# ---------------------------------------------------------------------------
# Study-block lifecycle and reason history (8.7)
# ---------------------------------------------------------------------------


def test_lifecycle_transitions() -> None:
    assert next_study_block_status(StudyBlockStatus.PLANNED, StudyBlockAction.START) is StudyBlockStatus.ACTIVE
    assert next_study_block_status(StudyBlockStatus.ACTIVE, StudyBlockAction.COMPLETE) is StudyBlockStatus.DONE
    assert next_study_block_status(StudyBlockStatus.PLANNED, StudyBlockAction.SKIP) is StudyBlockStatus.SKIPPED
    assert next_study_block_status(StudyBlockStatus.PLANNED, StudyBlockAction.EDIT) is StudyBlockStatus.PLANNED


def test_invalid_lifecycle_transition_raises() -> None:
    with pytest.raises(InvalidStudyBlockTransitionError):
        next_study_block_status(StudyBlockStatus.DONE, StudyBlockAction.START)


def test_reason_history_preserves_original() -> None:
    original = ReasonRecord(
        reason_json={"rule_version": PLANNER_RULE_VERSION, "selected_score": 100},
        reason_text="Original planning reason.",
        recorded_at=NOW,
        origin="plan",
    )
    edited = ReasonRecord(
        reason_json={"note": "rescheduled by learner"},
        reason_text="Rescheduled to the afternoon.",
        recorded_at=NOW + timedelta(hours=1),
        origin="reschedule",
    )
    history = append_reason_history((original,), edited)
    assert history[0] is original
    assert history[-1] is edited
    assert len(history) == 2
