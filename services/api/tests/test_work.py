"""Unit and integration tests for subjects, assignments, tasks, effort, goals.

Covers Requirement 7 (schoolwork management) and Requirement 24.2 (manual and
pasted/uploaded brief intake): subject kinds and archiving, assignment field
validation and lifecycle transitions, edit/reschedule, deletion audit/source
preservation, effort, editable-but-unpersisted drafts, idempotent confirmed
task creation, goals, and observer read-only enforcement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.domain.identity import (
    LEARNER_OWNER_SCOPES,
    ActorContext,
    AuthorizationError,
    Role,
    ValidationError,
)
from app.domain.work import (
    AssignmentAction,
    AssignmentStatus,
    BriefIntake,
    GoalStatus,
    IntakeMethod,
    InvalidTransitionError,
    NotFoundError,
    SourceKind,
    SubjectKind,
    next_assignment_status,
    normalize_draft,
)
from app.persistence.models import assignment_tasks, audit_records, metadata, sources
from app.persistence.seeds import LOCAL_LEARNER_ID, LOCAL_PARENT_ID, seed_local_personas
from app.services.work import WorkService

FIXED_NOW = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
DUE = datetime(2025, 2, 1, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        seed_local_personas(connection)
    return engine


def _service(engine) -> WorkService:
    return WorkService(engine, clock=FixedClock(FIXED_NOW))


LEARNER = ActorContext(
    actor_id=LOCAL_LEARNER_ID,
    owner_id=LOCAL_LEARNER_ID,
    role=Role.LEARNER,
    scopes=LEARNER_OWNER_SCOPES,
)
OBSERVER = ActorContext(
    actor_id=LOCAL_PARENT_ID,
    owner_id=LOCAL_LEARNER_ID,
    role=Role.PARENT,
    scopes=frozenset({"assignments:read"}),
)


def _subject(service: WorkService, slug: str = "algebra") -> UUID:
    subject = service.create_subject(LEARNER, slug=slug, title="Algebra")
    return subject.id


def _assignment(service: WorkService, subject_id: UUID, **overrides):
    kwargs = {
        "subject_id": subject_id,
        "title": "Fractions worksheet",
        "due_at": DUE,
        "estimated_minutes": 45,
    }
    kwargs.update(overrides)
    return service.create_assignment(LEARNER, **kwargs)


# ---------------------------------------------------------------------------
# Pure domain rules
# ---------------------------------------------------------------------------


def test_assignment_transitions_follow_lifecycle() -> None:
    assert next_assignment_status(AssignmentStatus.PENDING, AssignmentAction.START) is AssignmentStatus.IN_PROGRESS
    assert next_assignment_status(AssignmentStatus.IN_PROGRESS, AssignmentAction.COMPLETE) is AssignmentStatus.DONE
    assert next_assignment_status(AssignmentStatus.PENDING, AssignmentAction.COMPLETE) is AssignmentStatus.DONE
    assert next_assignment_status(AssignmentStatus.DONE, AssignmentAction.ARCHIVE) is AssignmentStatus.ARCHIVED


def test_invalid_transitions_raise() -> None:
    with pytest.raises(InvalidTransitionError):
        next_assignment_status(AssignmentStatus.DONE, AssignmentAction.START)
    with pytest.raises(InvalidTransitionError):
        next_assignment_status(AssignmentStatus.ARCHIVED, AssignmentAction.COMPLETE)


def test_normalize_draft_reorders_and_validates() -> None:
    assignment_id = uuid4()
    draft = normalize_draft(
        assignment_id,
        [
            {"title": "Second", "estimated_minutes": 20, "position": 5},
            {"title": "First", "estimated_minutes": 10, "position": 1},
        ],
        provenance={"model": "test"},
    )
    assert [task.title for task in draft.tasks] == ["First", "Second"]
    assert [task.position for task in draft.tasks] == [0, 1]
    assert draft.provenance == {"model": "test"}


def test_normalize_draft_rejects_empty_and_bad_fields() -> None:
    with pytest.raises(ValidationError):
        normalize_draft(uuid4(), [])
    with pytest.raises(ValidationError):
        normalize_draft(uuid4(), [{"title": "  ", "estimated_minutes": 10}])
    with pytest.raises(ValidationError):
        normalize_draft(uuid4(), [{"title": "ok", "estimated_minutes": 0}])


# ---------------------------------------------------------------------------
# Subjects (Requirement 7.1)
# ---------------------------------------------------------------------------


def test_subject_kinds_and_archive() -> None:
    service = _service(_engine())
    school = service.create_subject(
        LEARNER, slug="history", title="History", kind=SubjectKind.SCHOOL_MANAGED
    )
    learner_made = service.create_subject(
        LEARNER, slug="chess", title="Chess", kind=SubjectKind.LEARNER_CREATED
    )
    assert school.kind is SubjectKind.SCHOOL_MANAGED
    assert learner_made.owner_user_id == LOCAL_LEARNER_ID

    archived = service.archive_subject(LEARNER, learner_made.id)
    assert archived.is_archived is True

    active_only = service.list_subjects(LEARNER, include_archived=False)
    assert learner_made.id not in {s.id for s in active_only}
    assert school.id in {s.id for s in active_only}


def test_subject_kind_and_slug_validation() -> None:
    service = _service(_engine())
    with pytest.raises(ValidationError):
        service.create_subject(LEARNER, slug="math", title="Math", kind=SubjectKind.CURRICULUM)
    with pytest.raises(ValidationError):
        service.create_subject(LEARNER, slug="  ", title="Math")
    service.create_subject(LEARNER, slug="math", title="Math")
    with pytest.raises(ValidationError):
        service.create_subject(LEARNER, slug="math", title="Duplicate")


# ---------------------------------------------------------------------------
# Assignment creation and validation (Requirements 7.2, 7.12, 24.2)
# ---------------------------------------------------------------------------


def test_create_assignment_defaults_to_pending() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    assert assignment.status is AssignmentStatus.PENDING
    assert assignment.owner_user_id == LOCAL_LEARNER_ID
    assert assignment.brief_source_id is None


def test_create_assignment_with_pasted_brief_records_source() -> None:
    engine = _engine()
    service = _service(engine)
    subject_id = _subject(service)
    brief = BriefIntake(method=IntakeMethod.PASTED, content="Solve problems 1-10", title="Brief")
    assignment = _assignment(service, subject_id, brief=brief)
    assert assignment.brief_source_id is not None
    with engine.connect() as connection:
        row = connection.execute(
            select(sources).where(sources.c.id == assignment.brief_source_id)
        ).mappings().one()
        assert row["kind"] == SourceKind.ASSIGNMENT_BRIEF.value
        assert row["provenance"]["intake_method"] == "pasted"


def test_create_assignment_field_validation_errors() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    with pytest.raises(ValidationError) as bad_title:
        _assignment(service, subject_id, title="   ")
    assert bad_title.value.field == "title"
    with pytest.raises(ValidationError) as bad_effort:
        _assignment(service, subject_id, estimated_minutes=0)
    assert bad_effort.value.field == "estimated_minutes"
    with pytest.raises(ValidationError):
        _assignment(service, subject_id, due_at=datetime(2025, 2, 1, 12, 0))  # naive


def test_create_assignment_rejects_unknown_or_archived_subject() -> None:
    service = _service(_engine())
    with pytest.raises(NotFoundError):
        _assignment(service, uuid4())
    subject_id = _subject(service)
    service.archive_subject(LEARNER, subject_id)
    with pytest.raises(ValidationError):
        _assignment(service, subject_id)


def test_create_assignment_does_not_change_state_on_validation_error() -> None:
    engine = _engine()
    service = _service(engine)
    subject_id = _subject(service)
    with pytest.raises(ValidationError):
        _assignment(service, subject_id, estimated_minutes=-1)
    assert service.list_assignments(LEARNER) == []


# ---------------------------------------------------------------------------
# Lifecycle transitions (Requirements 7.3, 7.4, 7.5, 7.6)
# ---------------------------------------------------------------------------


def test_assignment_lifecycle_transitions() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)

    started = service.start_assignment(LEARNER, assignment.id)
    assert started.status is AssignmentStatus.IN_PROGRESS
    done = service.complete_assignment(LEARNER, assignment.id)
    assert done.status is AssignmentStatus.DONE
    archived = service.archive_assignment(LEARNER, assignment.id)
    assert archived.status is AssignmentStatus.ARCHIVED


def test_invalid_lifecycle_transition_is_typed_error() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    service.complete_assignment(LEARNER, assignment.id)
    with pytest.raises(InvalidTransitionError):
        service.start_assignment(LEARNER, assignment.id)


def test_edit_and_reschedule_persists_changed_fields() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    new_due = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
    updated = service.edit_assignment(
        LEARNER, assignment.id, title="Renamed", due_at=new_due, estimated_minutes=90
    )
    assert updated.title == "Renamed"
    assert updated.due_at == new_due
    assert updated.estimated_minutes == 90


# ---------------------------------------------------------------------------
# Deletion audit/source preservation (Requirement 7.14)
# ---------------------------------------------------------------------------


def test_delete_assignment_preserves_audit_and_source() -> None:
    engine = _engine()
    service = _service(engine)
    subject_id = _subject(service)
    brief = BriefIntake(method=IntakeMethod.PASTED, content="Do the reading")
    assignment = _assignment(service, subject_id, brief=brief)

    service.delete_assignment(LEARNER, assignment.id)

    # Excluded from active work views (7.14) but audit + source retained.
    assert service.list_assignments(LEARNER) == []
    assert service.list_assignments(LEARNER, include_deleted=True)[0].is_deleted is True
    with engine.connect() as connection:
        source_count = connection.scalar(
            select(func.count()).select_from(sources).where(sources.c.id == assignment.brief_source_id)
        )
        assert source_count == 1
        delete_audits = connection.scalar(
            select(func.count()).select_from(audit_records).where(audit_records.c.action == "assignment.delete")
        )
        assert delete_audits == 1


# ---------------------------------------------------------------------------
# Effort (Requirement 7.7)
# ---------------------------------------------------------------------------


def test_record_effort_requires_target_and_positive_minutes() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    service.record_effort(LEARNER, minutes=30, assignment_id=assignment.id)
    with pytest.raises(ValidationError):
        service.record_effort(LEARNER, minutes=30)
    with pytest.raises(ValidationError):
        service.record_effort(LEARNER, minutes=0, assignment_id=assignment.id)


# ---------------------------------------------------------------------------
# Task breakdown drafts (Requirements 7.8, 7.9, 7.10, 7.11)
# ---------------------------------------------------------------------------


def test_draft_task_breakdown_is_not_persisted() -> None:
    engine = _engine()
    service = _service(engine)
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)

    draft = service.draft_task_breakdown(
        LEARNER,
        assignment.id,
        [{"title": "Read chapter", "estimated_minutes": 20}],
        provenance={"model": "gpt-test"},
    )
    assert len(draft.tasks) == 1
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(assignment_tasks))
        assert count == 0


def test_confirm_task_breakdown_persists_once_and_is_idempotent() -> None:
    engine = _engine()
    service = _service(engine)
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    tasks = [
        {"title": "Read chapter", "estimated_minutes": 20},
        {"title": "Solve problems", "estimated_minutes": 25},
    ]

    first = service.confirm_task_breakdown(
        LEARNER, assignment.id, tasks, idempotency_key="tasks-1"
    )
    assert first.created is True
    assert len(first.tasks) == 2

    replay = service.confirm_task_breakdown(
        LEARNER, assignment.id, tasks, idempotency_key="tasks-1"
    )
    assert replay.created is False
    assert len(replay.tasks) == 2

    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(assignment_tasks))
        assert count == 2


def test_confirm_task_breakdown_rejects_reused_key_with_different_payload() -> None:
    from app.repositories.errors import IdempotencyKeyConflict

    service = _service(_engine())
    subject_id = _subject(service)
    assignment = _assignment(service, subject_id)
    service.confirm_task_breakdown(
        LEARNER,
        assignment.id,
        [{"title": "A", "estimated_minutes": 10}],
        idempotency_key="tasks-1",
    )
    with pytest.raises(IdempotencyKeyConflict):
        service.confirm_task_breakdown(
            LEARNER,
            assignment.id,
            [{"title": "B", "estimated_minutes": 10}],
            idempotency_key="tasks-1",
        )


# ---------------------------------------------------------------------------
# Goals (Requirement 7.13)
# ---------------------------------------------------------------------------


def test_goals_subject_scoped_and_cross_subject() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    subject_goal = service.create_goal(
        LEARNER, title="Master fractions", subject_id=subject_id, target_at=DUE
    )
    cross_goal = service.create_goal(LEARNER, title="Study daily")
    assert subject_goal.subject_id == subject_id
    assert cross_goal.subject_id is None

    completed = service.set_goal_status(LEARNER, subject_goal.id, GoalStatus.COMPLETED)
    assert completed.status is GoalStatus.COMPLETED
    assert {g.id for g in service.list_goals(LEARNER)} == {subject_goal.id, cross_goal.id}


# ---------------------------------------------------------------------------
# Observer read-only enforcement (Requirement 2.6)
# ---------------------------------------------------------------------------


def test_observer_cannot_mutate_learner_work() -> None:
    service = _service(_engine())
    subject_id = _subject(service)
    with pytest.raises(AuthorizationError):
        service.create_assignment(
            OBSERVER,
            subject_id=subject_id,
            title="Attempt",
            due_at=DUE,
            estimated_minutes=30,
        )
    with pytest.raises(AuthorizationError):
        service.create_subject(OBSERVER, slug="x", title="X")
