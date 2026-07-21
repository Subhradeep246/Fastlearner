"""Subjects, assignments, tasks, effort, goals, and source-brief domain model.

This module holds the pure value objects, entities, lifecycle enumerations,
transition rules, validation, and typed domain errors for schoolwork
management (Requirement 7 and Requirement 24.2). It contains no persistence,
framework, or provider dependencies so the rules stay independently testable.

Key rules encoded here:

* Subjects may be school-managed, learner-created, curriculum, or archived
  (Requirement 7.1).
* Assignments capture subject, title, due date, estimated effort, a pending
  status, and an optional brief/rubric source (Requirement 7.2).
* Assignment status follows explicit lifecycle transitions (Requirements 7.3,
  7.4, 7.5).
* Extracted task breakdowns are editable drafts that stay outside canonical
  state until confirmed (Requirements 7.8, 7.9).
* Required assignment data is validated with field-specific errors before any
  canonical change (Requirement 7.12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.identity import IdentityError, ValidationError

# Re-exported so callers work against one error vocabulary.
__all__ = [
    "AssignmentAction",
    "AssignmentStatus",
    "Assignment",
    "AssignmentTask",
    "BriefIntake",
    "DraftTask",
    "EffortEntry",
    "Goal",
    "GoalStatus",
    "IntakeMethod",
    "InvalidTransitionError",
    "NotFoundError",
    "Source",
    "SourceKind",
    "Subject",
    "SubjectKind",
    "SubjectStatus",
    "TaskBreakdownDraft",
    "TaskStatus",
    "ValidationError",
    "next_assignment_status",
    "normalize_draft",
    "validate_estimated_minutes",
    "validate_future_or_any_datetime",
    "validate_title",
]

# Titles and other short free-text fields share these bounds; they mirror the
# persistence column limits so validation fails before a database round-trip.
_MAX_TITLE = 240
_MAX_SLUG = 96


# ---------------------------------------------------------------------------
# Lifecycle enumerations (mirrored by database check constraints)
# ---------------------------------------------------------------------------


class SubjectKind(StrEnum):
    CURRICULUM = "curriculum"
    SCHOOL_MANAGED = "school_managed"
    LEARNER_CREATED = "learner_created"


class SubjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ARCHIVED = "archived"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class SourceKind(StrEnum):
    """Origin classification for a saved brief or rubric Source_Record."""

    ASSIGNMENT_BRIEF = "assignment_brief"
    ASSIGNMENT_RUBRIC = "assignment_rubric"


class IntakeMethod(StrEnum):
    """Supported initial-release assignment-brief intake (Requirement 24.2)."""

    MANUAL = "manual"
    PASTED = "pasted"
    UPLOADED = "uploaded"


class AssignmentAction(StrEnum):
    """A learner-driven assignment lifecycle action."""

    START = "start"
    COMPLETE = "complete"
    ARCHIVE = "archive"


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class NotFoundError(IdentityError):
    """A requested resource is absent within the authorized owner scope.

    The message is deliberately scope-safe so a foreign record and an absent
    record are indistinguishable to the caller.
    """

    code = "not_found"

    def __init__(self, message: str = "The requested resource was not found.") -> None:
        super().__init__(message)


class InvalidTransitionError(IdentityError):
    """A lifecycle transition is not permitted from the current status."""

    code = "invalid_state_transition"

    def __init__(self, resource_kind: str, current: str, action: str) -> None:
        super().__init__(
            f"Cannot {action} a {resource_kind} in '{current}' status."
        )
        self.resource_kind = resource_kind
        self.current = current
        self.action = action


# ---------------------------------------------------------------------------
# Value objects and entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """A subject the learner can organize schoolwork under.

    ``owner_user_id`` is ``None`` for shared curriculum subjects and set to the
    learner owner for school-managed and learner-created subjects.
    """

    id: UUID
    owner_user_id: UUID | None
    slug: str
    title: str
    kind: SubjectKind
    status: SubjectStatus = SubjectStatus.ACTIVE
    archived_at: datetime | None = None

    @property
    def is_archived(self) -> bool:
        return self.status is SubjectStatus.ARCHIVED

    def is_accessible_to(self, owner_id: UUID) -> bool:
        """Curriculum subjects are shared; owned subjects require a match."""
        return self.owner_user_id is None or self.owner_user_id == owner_id


@dataclass(frozen=True)
class Source:
    """Metadata identifying the origin of a saved brief or rubric."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID | None
    kind: SourceKind
    content_checksum: str
    provenance: dict[str, Any]
    title: str | None = None
    uri: str | None = None
    status: str = "active"
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class BriefIntake:
    """Manual, pasted, or uploaded assignment-brief intake (Requirement 24.2)."""

    method: IntakeMethod
    kind: SourceKind = SourceKind.ASSIGNMENT_BRIEF
    content: str | None = None
    title: str | None = None
    uri: str | None = None


@dataclass(frozen=True)
class Assignment:
    """A schoolwork assignment with lifecycle status (Requirement 7.2)."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID
    title: str
    due_at: datetime
    estimated_minutes: int
    status: AssignmentStatus = AssignmentStatus.PENDING
    brief_source_id: UUID | None = None
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True)
class AssignmentTask:
    """A confirmed, persisted subtask of an assignment breakdown."""

    id: UUID
    owner_user_id: UUID
    assignment_id: UUID
    title: str
    position: int
    estimated_minutes: int
    due_at: datetime | None = None
    status: TaskStatus = TaskStatus.PENDING


@dataclass(frozen=True)
class DraftTask:
    """A single editable task inside an unconfirmed breakdown draft."""

    title: str
    estimated_minutes: int
    position: int
    due_at: datetime | None = None


@dataclass(frozen=True)
class TaskBreakdownDraft:
    """An editable extraction draft that stays outside canonical state.

    While a breakdown is a draft it is never persisted to canonical tables
    (Requirement 7.9). The draft carries generation provenance so a later
    confirmation records how the tasks were produced (Requirement 7.8).
    """

    assignment_id: UUID
    tasks: tuple[DraftTask, ...]
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EffortEntry:
    """Actual effort recorded against an assignment or completed study block."""

    id: UUID
    owner_user_id: UUID
    minutes: int
    recorded_at: datetime
    assignment_id: UUID | None = None
    study_block_id: UUID | None = None


@dataclass(frozen=True)
class Goal:
    """A subject-scoped or cross-subject goal (Requirement 7.13)."""

    id: UUID
    owner_user_id: UUID
    title: str
    subject_id: UUID | None = None
    target_at: datetime | None = None
    status: GoalStatus = GoalStatus.ACTIVE


# ---------------------------------------------------------------------------
# Pure validation and transition rules
# ---------------------------------------------------------------------------


def validate_title(value: str, *, field_name: str = "title", max_length: int = _MAX_TITLE) -> str:
    """Trim and validate a required free-text title (Requirement 7.12)."""
    clean = (value or "").strip()
    if not clean:
        raise ValidationError(f"{field_name.replace('_', ' ').capitalize()} is required.", field=field_name)
    if len(clean) > max_length:
        raise ValidationError(
            f"{field_name.replace('_', ' ').capitalize()} must be at most {max_length} characters.",
            field=field_name,
        )
    return clean


def validate_slug(value: str) -> str:
    clean = (value or "").strip().lower()
    if not clean:
        raise ValidationError("Subject slug is required.", field="slug")
    if len(clean) > _MAX_SLUG:
        raise ValidationError(f"Subject slug must be at most {_MAX_SLUG} characters.", field="slug")
    if not all(ch.isalnum() or ch in "-_" for ch in clean):
        raise ValidationError(
            "Subject slug may only contain letters, numbers, hyphens, and underscores.",
            field="slug",
        )
    return clean


def validate_estimated_minutes(value: Any, *, field_name: str = "estimated_minutes") -> int:
    """Estimated effort must be a positive integer count of minutes."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("Estimated effort must be an integer number of minutes.", field=field_name)
    minutes: int = value
    if minutes <= 0:
        raise ValidationError("Estimated effort must be greater than zero minutes.", field=field_name)
    return minutes


def validate_positive_minutes(value: Any, *, field_name: str = "minutes") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("Minutes must be an integer.", field=field_name)
    minutes: int = value
    if minutes <= 0:
        raise ValidationError("Minutes must be greater than zero.", field=field_name)
    return minutes


def validate_future_or_any_datetime(value: datetime, *, field_name: str) -> datetime:
    """Require a timezone-aware instant; UTC normalization happens at the edge."""
    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name.replace('_', ' ').capitalize()} must be a timestamp.", field=field_name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(
            f"{field_name.replace('_', ' ').capitalize()} must include a UTC offset.",
            field=field_name,
        )
    return value


#: Allowed assignment lifecycle transitions per learner action.
_ASSIGNMENT_TRANSITIONS: dict[AssignmentAction, tuple[frozenset[AssignmentStatus], AssignmentStatus]] = {
    AssignmentAction.START: (
        frozenset({AssignmentStatus.PENDING}),
        AssignmentStatus.IN_PROGRESS,
    ),
    AssignmentAction.COMPLETE: (
        frozenset({AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS}),
        AssignmentStatus.DONE,
    ),
    AssignmentAction.ARCHIVE: (
        frozenset(
            {AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS, AssignmentStatus.DONE}
        ),
        AssignmentStatus.ARCHIVED,
    ),
}


def next_assignment_status(current: AssignmentStatus, action: AssignmentAction) -> AssignmentStatus:
    """Return the resulting status for a lifecycle ``action`` or raise.

    Encodes Requirements 7.3 (start: pending -> in_progress), 7.4 (complete ->
    done), and 7.5 (archive -> archived). Invalid transitions raise a typed
    :class:`InvalidTransitionError` and never change canonical state.
    """
    allowed_from, target = _ASSIGNMENT_TRANSITIONS[action]
    if current not in allowed_from:
        raise InvalidTransitionError("assignment", current.value, action.value)
    return target


def normalize_draft(
    assignment_id: UUID,
    tasks: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    provenance: dict[str, Any] | None = None,
) -> TaskBreakdownDraft:
    """Validate and normalize an editable task breakdown draft.

    The draft is a pure value object: normalization performs field validation
    and assigns contiguous ordering, but nothing here touches canonical state
    (Requirement 7.9). Confirmation is handled by the application service.
    """
    if not tasks:
        raise ValidationError("A task breakdown must contain at least one task.", field="tasks")

    normalized: list[DraftTask] = []
    for index, raw in enumerate(tasks):
        title = validate_title(str(raw.get("title", "")), field_name="tasks")
        minutes = validate_estimated_minutes(raw.get("estimated_minutes"), field_name="estimated_minutes")
        due_at = raw.get("due_at")
        if due_at is not None:
            due_at = validate_future_or_any_datetime(due_at, field_name="due_at")
        position = raw.get("position", index)
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValidationError("Task position must be a non-negative integer.", field="position")
        normalized.append(
            DraftTask(title=title, estimated_minutes=minutes, position=position, due_at=due_at)
        )

    # Re-sort by requested position then original order and re-index so the
    # persisted ordering is always contiguous and stable.
    normalized.sort(key=lambda task: task.position)
    ordered = tuple(
        DraftTask(
            title=task.title,
            estimated_minutes=task.estimated_minutes,
            position=position,
            due_at=task.due_at,
        )
        for position, task in enumerate(normalized)
    )
    return TaskBreakdownDraft(
        assignment_id=assignment_id,
        tasks=ordered,
        provenance=dict(provenance or {}),
    )
