"""Work repository port and SQLAlchemy Core implementation.

Covers subjects, assignments, assignment tasks, effort entries, goals, and
brief/rubric source records. Every owner-scoped query carries the resolved
owner id as a positional argument and never trusts a client-supplied owner
(Requirements 2.10, 17.11). Subjects are a shared/curriculum resource, so their
reads additionally allow rows with a ``NULL`` owner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.domain.work import (
    Assignment,
    AssignmentStatus,
    AssignmentTask,
    DraftTask,
    EffortEntry,
    Goal,
    GoalStatus,
    Source,
    SourceKind,
    Subject,
    SubjectKind,
    SubjectStatus,
    TaskStatus,
    ValidationError,
)
from app.persistence.models import (
    assignment_tasks,
    assignments,
    effort_entries,
    goals,
    sources,
    subjects,
    utc_datetime,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize DB timestamps to timezone-aware UTC (SQLite returns naive)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SubjectSlugConflict(ValidationError):
    """Raised when a subject slug is already used within the owner scope."""

    def __init__(self) -> None:
        super().__init__("A subject with this slug already exists.", field="slug")


class WorkRepository(Protocol):
    """Port for subjects, assignments, tasks, effort, goals, and sources."""

    # -- subjects ----------------------------------------------------------
    def create_subject(self, subject: Subject) -> Subject: ...

    def get_subject(self, owner_user_id: UUID, subject_id: UUID) -> Subject | None: ...

    def list_subjects(
        self, owner_user_id: UUID, *, include_archived: bool = True
    ) -> list[Subject]: ...

    def set_subject_status(
        self,
        owner_user_id: UUID,
        subject_id: UUID,
        status: SubjectStatus,
        *,
        archived_at: datetime | None = None,
    ) -> Subject | None: ...

    # -- sources -----------------------------------------------------------
    def create_source(self, source: Source) -> Source: ...

    def get_source(self, owner_user_id: UUID, source_id: UUID) -> Source | None: ...

    # -- assignments -------------------------------------------------------
    def create_assignment(self, assignment: Assignment) -> Assignment: ...

    def get_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, *, include_deleted: bool = False
    ) -> Assignment | None: ...

    def list_assignments(
        self, owner_user_id: UUID, *, include_deleted: bool = False
    ) -> list[Assignment]: ...

    def update_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, changes: dict[str, Any]
    ) -> Assignment | None: ...

    def soft_delete_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, at: datetime
    ) -> Assignment | None: ...

    # -- assignment tasks --------------------------------------------------
    def list_tasks(self, owner_user_id: UUID, assignment_id: UUID) -> list[AssignmentTask]: ...

    def replace_tasks(
        self, owner_user_id: UUID, assignment_id: UUID, tasks: list[DraftTask]
    ) -> list[AssignmentTask]: ...

    # -- effort ------------------------------------------------------------
    def add_effort_entry(self, entry: EffortEntry) -> EffortEntry: ...

    # -- goals -------------------------------------------------------------
    def create_goal(self, goal: Goal) -> Goal: ...

    def list_goals(self, owner_user_id: UUID) -> list[Goal]: ...

    def set_goal_status(
        self, owner_user_id: UUID, goal_id: UUID, status: GoalStatus
    ) -> Goal | None: ...


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _subject_from_row(row: Any) -> Subject:
    return Subject(
        id=row.id,
        owner_user_id=row.owner_user_id,
        slug=row.slug,
        title=row.title,
        kind=SubjectKind(row.kind),
        status=SubjectStatus(row.status),
        archived_at=_as_utc(row.archived_at),
    )


def _source_from_row(row: Any) -> Source:
    return Source(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        kind=SourceKind(row.kind),
        content_checksum=row.content_checksum,
        provenance=dict(row.provenance or {}),
        title=row.title,
        uri=row.uri,
        status=row.status,
        deleted_at=_as_utc(row.deleted_at),
    )


def _assignment_from_row(row: Any) -> Assignment:
    return Assignment(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        title=row.title,
        due_at=_as_utc(row.due_at),  # type: ignore[arg-type]
        estimated_minutes=row.estimated_minutes,
        status=AssignmentStatus(row.status),
        brief_source_id=row.brief_source_id,
        deleted_at=_as_utc(row.deleted_at),
    )


def _task_from_row(row: Any) -> AssignmentTask:
    return AssignmentTask(
        id=row.id,
        owner_user_id=row.owner_user_id,
        assignment_id=row.assignment_id,
        title=row.title,
        position=row.position,
        estimated_minutes=row.estimated_minutes,
        due_at=_as_utc(row.due_at),
        status=TaskStatus(row.status),
    )


def _goal_from_row(row: Any) -> Goal:
    return Goal(
        id=row.id,
        owner_user_id=row.owner_user_id,
        title=row.title,
        subject_id=row.subject_id,
        target_at=_as_utc(row.target_at),
        status=GoalStatus(row.status),
    )


def _effort_from_row(row: Any) -> EffortEntry:
    return EffortEntry(
        id=row.id,
        owner_user_id=row.owner_user_id,
        minutes=row.minutes,
        recorded_at=_as_utc(row.recorded_at),  # type: ignore[arg-type]
        assignment_id=row.assignment_id,
        study_block_id=row.study_block_id,
    )


class SqlWorkRepository(WorkRepository):
    """SQLAlchemy Core implementation backed by a live connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    # -- subjects ----------------------------------------------------------
    def create_subject(self, subject: Subject) -> Subject:
        try:
            self._connection.execute(
                subjects.insert().values(
                    id=subject.id,
                    owner_user_id=subject.owner_user_id,
                    slug=subject.slug,
                    title=subject.title,
                    kind=subject.kind.value,
                    status=subject.status.value,
                    archived_at=subject.archived_at,
                )
            )
        except IntegrityError as error:
            raise SubjectSlugConflict() from error
        stored = self.get_subject(subject.owner_user_id or subject.id, subject.id)
        assert stored is not None
        return stored

    def get_subject(self, owner_user_id: UUID, subject_id: UUID) -> Subject | None:
        row = self._connection.execute(
            select(subjects).where(
                and_(
                    subjects.c.id == subject_id,
                    or_(
                        subjects.c.owner_user_id == owner_user_id,
                        subjects.c.owner_user_id.is_(None),
                    ),
                )
            )
        ).first()
        return _subject_from_row(row) if row is not None else None

    def list_subjects(
        self, owner_user_id: UUID, *, include_archived: bool = True
    ) -> list[Subject]:
        predicate = or_(
            subjects.c.owner_user_id == owner_user_id,
            subjects.c.owner_user_id.is_(None),
        )
        if not include_archived:
            predicate = and_(predicate, subjects.c.status == SubjectStatus.ACTIVE.value)
        rows = self._connection.execute(
            select(subjects).where(predicate).order_by(subjects.c.title)
        ).all()
        return [_subject_from_row(row) for row in rows]

    def set_subject_status(
        self,
        owner_user_id: UUID,
        subject_id: UUID,
        status: SubjectStatus,
        *,
        archived_at: datetime | None = None,
    ) -> Subject | None:
        result = self._connection.execute(
            subjects.update()
            .where(
                and_(subjects.c.id == subject_id, subjects.c.owner_user_id == owner_user_id)
            )
            .values(
                status=status.value,
                archived_at=utc_datetime(archived_at) if archived_at is not None else None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount == 0:
            return None
        return self.get_subject(owner_user_id, subject_id)

    # -- sources -----------------------------------------------------------
    def create_source(self, source: Source) -> Source:
        self._connection.execute(
            sources.insert().values(
                id=source.id,
                owner_user_id=source.owner_user_id,
                subject_id=source.subject_id,
                kind=source.kind.value,
                title=source.title,
                uri=source.uri,
                content_checksum=source.content_checksum,
                provenance=dict(source.provenance),
                status=source.status,
                deleted_at=source.deleted_at,
            )
        )
        stored = self.get_source(source.owner_user_id, source.id)
        assert stored is not None
        return stored

    def get_source(self, owner_user_id: UUID, source_id: UUID) -> Source | None:
        row = self._connection.execute(
            select(sources).where(
                and_(sources.c.id == source_id, sources.c.owner_user_id == owner_user_id)
            )
        ).first()
        return _source_from_row(row) if row is not None else None

    # -- assignments -------------------------------------------------------
    def create_assignment(self, assignment: Assignment) -> Assignment:
        self._connection.execute(
            assignments.insert().values(
                id=assignment.id,
                owner_user_id=assignment.owner_user_id,
                subject_id=assignment.subject_id,
                title=assignment.title,
                due_at=utc_datetime(assignment.due_at),
                estimated_minutes=assignment.estimated_minutes,
                status=assignment.status.value,
                brief_source_id=assignment.brief_source_id,
                deleted_at=assignment.deleted_at,
            )
        )
        stored = self.get_assignment(
            assignment.owner_user_id, assignment.id, include_deleted=True
        )
        assert stored is not None
        return stored

    def get_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, *, include_deleted: bool = False
    ) -> Assignment | None:
        predicate = and_(
            assignments.c.id == assignment_id,
            assignments.c.owner_user_id == owner_user_id,
        )
        if not include_deleted:
            predicate = and_(predicate, assignments.c.deleted_at.is_(None))
        row = self._connection.execute(select(assignments).where(predicate)).first()
        return _assignment_from_row(row) if row is not None else None

    def list_assignments(
        self, owner_user_id: UUID, *, include_deleted: bool = False
    ) -> list[Assignment]:
        predicate = assignments.c.owner_user_id == owner_user_id
        if not include_deleted:
            predicate = and_(predicate, assignments.c.deleted_at.is_(None))
        rows = self._connection.execute(
            select(assignments).where(predicate).order_by(assignments.c.due_at)
        ).all()
        return [_assignment_from_row(row) for row in rows]

    def update_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, changes: dict[str, Any]
    ) -> Assignment | None:
        if not changes:
            return self.get_assignment(owner_user_id, assignment_id)
        values = dict(changes)
        if "due_at" in values and values["due_at"] is not None:
            values["due_at"] = utc_datetime(values["due_at"])
        values["updated_at"] = datetime.now(timezone.utc)
        result = self._connection.execute(
            assignments.update()
            .where(
                and_(
                    assignments.c.id == assignment_id,
                    assignments.c.owner_user_id == owner_user_id,
                    assignments.c.deleted_at.is_(None),
                )
            )
            .values(**values)
        )
        if result.rowcount == 0:
            return None
        return self.get_assignment(owner_user_id, assignment_id)

    def soft_delete_assignment(
        self, owner_user_id: UUID, assignment_id: UUID, at: datetime
    ) -> Assignment | None:
        result = self._connection.execute(
            assignments.update()
            .where(
                and_(
                    assignments.c.id == assignment_id,
                    assignments.c.owner_user_id == owner_user_id,
                    assignments.c.deleted_at.is_(None),
                )
            )
            .values(deleted_at=utc_datetime(at), updated_at=datetime.now(timezone.utc))
        )
        if result.rowcount == 0:
            return None
        return self.get_assignment(owner_user_id, assignment_id, include_deleted=True)

    # -- assignment tasks --------------------------------------------------
    def list_tasks(self, owner_user_id: UUID, assignment_id: UUID) -> list[AssignmentTask]:
        rows = self._connection.execute(
            select(assignment_tasks)
            .where(
                and_(
                    assignment_tasks.c.owner_user_id == owner_user_id,
                    assignment_tasks.c.assignment_id == assignment_id,
                )
            )
            .order_by(assignment_tasks.c.position)
        ).all()
        return [_task_from_row(row) for row in rows]

    def replace_tasks(
        self, owner_user_id: UUID, assignment_id: UUID, tasks: list[DraftTask]
    ) -> list[AssignmentTask]:
        self._connection.execute(
            assignment_tasks.delete().where(
                and_(
                    assignment_tasks.c.owner_user_id == owner_user_id,
                    assignment_tasks.c.assignment_id == assignment_id,
                )
            )
        )
        for task in tasks:
            self._connection.execute(
                assignment_tasks.insert().values(
                    id=uuid4(),
                    owner_user_id=owner_user_id,
                    assignment_id=assignment_id,
                    title=task.title,
                    position=task.position,
                    estimated_minutes=task.estimated_minutes,
                    due_at=utc_datetime(task.due_at) if task.due_at is not None else None,
                    status=TaskStatus.PENDING.value,
                )
            )
        return self.list_tasks(owner_user_id, assignment_id)

    # -- effort ------------------------------------------------------------
    def add_effort_entry(self, entry: EffortEntry) -> EffortEntry:
        self._connection.execute(
            effort_entries.insert().values(
                id=entry.id,
                owner_user_id=entry.owner_user_id,
                assignment_id=entry.assignment_id,
                study_block_id=entry.study_block_id,
                minutes=entry.minutes,
                recorded_at=utc_datetime(entry.recorded_at),
            )
        )
        row = self._connection.execute(
            select(effort_entries).where(effort_entries.c.id == entry.id)
        ).first()
        assert row is not None
        return _effort_from_row(row)

    # -- goals -------------------------------------------------------------
    def create_goal(self, goal: Goal) -> Goal:
        self._connection.execute(
            goals.insert().values(
                id=goal.id,
                owner_user_id=goal.owner_user_id,
                subject_id=goal.subject_id,
                title=goal.title,
                target_at=utc_datetime(goal.target_at) if goal.target_at is not None else None,
                status=goal.status.value,
            )
        )
        stored = self._get_goal(goal.owner_user_id, goal.id)
        assert stored is not None
        return stored

    def list_goals(self, owner_user_id: UUID) -> list[Goal]:
        rows = self._connection.execute(
            select(goals)
            .where(goals.c.owner_user_id == owner_user_id)
            .order_by(goals.c.created_at)
        ).all()
        return [_goal_from_row(row) for row in rows]

    def set_goal_status(
        self, owner_user_id: UUID, goal_id: UUID, status: GoalStatus
    ) -> Goal | None:
        result = self._connection.execute(
            goals.update()
            .where(and_(goals.c.id == goal_id, goals.c.owner_user_id == owner_user_id))
            .values(status=status.value, updated_at=datetime.now(timezone.utc))
        )
        if result.rowcount == 0:
            return None
        return self._get_goal(owner_user_id, goal_id)

    def _get_goal(self, owner_user_id: UUID, goal_id: UUID) -> Goal | None:
        row = self._connection.execute(
            select(goals).where(
                and_(goals.c.id == goal_id, goals.c.owner_user_id == owner_user_id)
            )
        ).first()
        return _goal_from_row(row) if row is not None else None
