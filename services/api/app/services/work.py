"""Subjects, assignments, tasks, effort, goals, and source-brief use cases.

This application service orchestrates the transactional boundary for schoolwork
management (Requirement 7 and Requirement 24.2). It composes the durable
workflow primitives established earlier: a unit of work for atomic commits, an
audit trail for confirmed mutations, operation-scoped idempotency records for
duplicate-safe task creation, and the transactional outbox for follow-up work.

Ownership rules:

* The effective owner scope is taken from the resolved :class:`ActorContext`
  and never from a client-supplied owner identifier (Requirements 2.10, 17.11).
* Observers can read but never mutate learner work (Requirement 2.6).

State rules:

* Field validation raises typed, field-specific errors before any canonical
  change (Requirement 7.12).
* Extracted task breakdowns stay outside canonical state until confirmed
  (Requirements 7.8, 7.9); confirmation persists exactly once (Requirements
  7.10, 7.11).
* Deleting an assignment preserves audit and brief Source_Record lifecycle
  information while excluding it from active work views (Requirement 7.14).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine

from app.clock import Clock, system_clock
from app.domain.identity import ActorContext, AuthorizationError
from app.domain.work import (
    Assignment,
    AssignmentAction,
    AssignmentStatus,
    AssignmentTask,
    BriefIntake,
    EffortEntry,
    Goal,
    GoalStatus,
    IntakeMethod,
    NotFoundError,
    Source,
    Subject,
    SubjectKind,
    SubjectStatus,
    TaskBreakdownDraft,
    ValidationError,
    next_assignment_status,
    normalize_draft,
    validate_estimated_minutes,
    validate_future_or_any_datetime,
    validate_positive_minutes,
    validate_slug,
    validate_title,
)
from app.repositories.idempotency import hash_request
from app.repositories.unit_of_work import SqlUnitOfWork
from app.repositories.work import SqlWorkRepository, WorkRepository

RepositoryFactory = Callable[[Connection], WorkRepository]

_CONFIRM_TASKS_OPERATION = "assignment_tasks.confirm"
_CREATE_ASSIGNMENT_OPERATION = "assignments.create"

# Subjects a learner may create directly; curriculum subjects are seeded.
_CREATABLE_SUBJECT_KINDS = frozenset({SubjectKind.SCHOOL_MANAGED, SubjectKind.LEARNER_CREATED})


@dataclass(frozen=True)
class TaskConfirmation:
    """Result of confirming a task breakdown.

    ``created`` is ``False`` when a repeated idempotency key replayed the
    original outcome without creating duplicate tasks (Requirement 7.11).
    """

    assignment_id: UUID
    tasks: tuple[AssignmentTask, ...]
    created: bool


def _now(clock: Clock, at: datetime | None) -> datetime:
    return at if at is not None else clock()


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkService:
    """Use cases for subjects, assignments, tasks, effort, and goals."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock = system_clock,
        repository_factory: RepositoryFactory = SqlWorkRepository,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._repository_factory = repository_factory

    # ------------------------------------------------------------------
    # Subjects (Requirement 7.1)
    # ------------------------------------------------------------------
    def create_subject(
        self,
        actor: ActorContext,
        *,
        slug: str,
        title: str,
        kind: SubjectKind = SubjectKind.LEARNER_CREATED,
        subject_id: UUID | None = None,
        request_id: str | UUID | None = None,
    ) -> Subject:
        self._require_owner(actor)
        if kind not in _CREATABLE_SUBJECT_KINDS:
            raise ValidationError(
                "Only school-managed or learner-created subjects can be created.",
                field="kind",
            )
        clean_slug = validate_slug(slug)
        clean_title = validate_title(title, field_name="title", max_length=160)
        subject = Subject(
            id=subject_id or uuid4(),
            owner_user_id=actor.owner_id,
            slug=clean_slug,
            title=clean_title,
            kind=kind,
            status=SubjectStatus.ACTIVE,
        )
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            stored = repo.create_subject(subject)
            self._audit(uow, actor, "subject.create", "subject", stored.id, request_id)
            uow.commit()
        return stored

    def list_subjects(self, actor: ActorContext, *, include_archived: bool = True) -> list[Subject]:
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            subjects = repo.list_subjects(actor.owner_id, include_archived=include_archived)
            uow.rollback()
        return subjects

    def archive_subject(
        self,
        actor: ActorContext,
        subject_id: UUID,
        *,
        request_id: str | UUID | None = None,
        at: datetime | None = None,
    ) -> Subject:
        self._require_owner(actor)
        moment = _now(self._clock, at)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            updated = repo.set_subject_status(
                actor.owner_id, subject_id, SubjectStatus.ARCHIVED, archived_at=moment
            )
            if updated is None:
                raise NotFoundError("The requested subject was not found.")
            self._audit(uow, actor, "subject.archive", "subject", subject_id, request_id)
            uow.commit()
        return updated

    # ------------------------------------------------------------------
    # Assignments (Requirements 7.2, 7.3, 7.4, 7.5, 7.6, 7.12, 7.14, 24.2)
    # ------------------------------------------------------------------
    def create_assignment(
        self,
        actor: ActorContext,
        *,
        subject_id: UUID,
        title: str,
        due_at: datetime,
        estimated_minutes: int,
        brief: BriefIntake | None = None,
        assignment_id: UUID | None = None,
        idempotency_key: str | None = None,
        request_id: str | UUID | None = None,
        at: datetime | None = None,
    ) -> Assignment:
        self._require_owner(actor)
        clean_title = validate_title(title)
        clean_due = validate_future_or_any_datetime(due_at, field_name="due_at")
        clean_minutes = validate_estimated_minutes(estimated_minutes)
        new_id = assignment_id or uuid4()

        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)

            if idempotency_key:
                request_hash = hash_request(
                    {
                        "subject_id": str(subject_id),
                        "title": clean_title,
                        "due_at": clean_due,
                        "estimated_minutes": clean_minutes,
                    }
                )
                claim = uow.idempotency.begin(
                    owner_user_id=actor.owner_id,
                    operation=_CREATE_ASSIGNMENT_OPERATION,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if claim.completed and claim.outcome and claim.outcome.result_ref is not None:
                    existing = repo.get_assignment(
                        actor.owner_id, claim.outcome.result_ref, include_deleted=True
                    )
                    uow.rollback()
                    if existing is None:  # pragma: no cover - defensive
                        raise NotFoundError("The original assignment was not found.")
                    return existing

            subject = repo.get_subject(actor.owner_id, subject_id)
            if subject is None:
                raise NotFoundError("The requested subject was not found.")
            if subject.is_archived:
                raise ValidationError(
                    "Assignments cannot be added to an archived subject.", field="subject_id"
                )

            brief_source_id: UUID | None = None
            if brief is not None:
                source = self._build_brief_source(actor.owner_id, subject_id, brief, _now(self._clock, at))
                stored_source = repo.create_source(source)
                brief_source_id = stored_source.id

            assignment = Assignment(
                id=new_id,
                owner_user_id=actor.owner_id,
                subject_id=subject_id,
                title=clean_title,
                due_at=clean_due,
                estimated_minutes=clean_minutes,
                status=AssignmentStatus.PENDING,
                brief_source_id=brief_source_id,
            )
            stored = repo.create_assignment(assignment)
            self._audit(
                uow,
                actor,
                "assignment.create",
                "assignment",
                stored.id,
                request_id,
                details={"brief_source_id": str(brief_source_id) if brief_source_id else None},
            )
            if idempotency_key:
                uow.idempotency.complete(
                    owner_user_id=actor.owner_id,
                    operation=_CREATE_ASSIGNMENT_OPERATION,
                    key=idempotency_key,
                    response_status=201,
                    result_ref=stored.id,
                )
            uow.commit()
        return stored

    def get_assignment(self, actor: ActorContext, assignment_id: UUID) -> Assignment:
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            assignment = repo.get_assignment(actor.owner_id, assignment_id)
            uow.rollback()
        if assignment is None:
            raise NotFoundError("The requested assignment was not found.")
        return assignment

    def list_assignments(
        self, actor: ActorContext, *, include_deleted: bool = False
    ) -> list[Assignment]:
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            items = repo.list_assignments(actor.owner_id, include_deleted=include_deleted)
            uow.rollback()
        return items

    def start_assignment(
        self, actor: ActorContext, assignment_id: UUID, *, request_id: str | UUID | None = None
    ) -> Assignment:
        return self._transition_assignment(actor, assignment_id, AssignmentAction.START, request_id)

    def complete_assignment(
        self, actor: ActorContext, assignment_id: UUID, *, request_id: str | UUID | None = None
    ) -> Assignment:
        return self._transition_assignment(actor, assignment_id, AssignmentAction.COMPLETE, request_id)

    def archive_assignment(
        self, actor: ActorContext, assignment_id: UUID, *, request_id: str | UUID | None = None
    ) -> Assignment:
        return self._transition_assignment(actor, assignment_id, AssignmentAction.ARCHIVE, request_id)

    def edit_assignment(
        self,
        actor: ActorContext,
        assignment_id: UUID,
        *,
        title: str | None = None,
        due_at: datetime | None = None,
        estimated_minutes: int | None = None,
        subject_id: UUID | None = None,
        request_id: str | UUID | None = None,
    ) -> Assignment:
        """Edit or reschedule an assignment, persisting changed fields (7.6)."""
        self._require_owner(actor)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = validate_title(title)
        if due_at is not None:
            changes["due_at"] = validate_future_or_any_datetime(due_at, field_name="due_at")
        if estimated_minutes is not None:
            changes["estimated_minutes"] = validate_estimated_minutes(estimated_minutes)

        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            current = repo.get_assignment(actor.owner_id, assignment_id)
            if current is None:
                raise NotFoundError("The requested assignment was not found.")
            if subject_id is not None and subject_id != current.subject_id:
                subject = repo.get_subject(actor.owner_id, subject_id)
                if subject is None:
                    raise NotFoundError("The requested subject was not found.")
                if subject.is_archived:
                    raise ValidationError(
                        "Assignments cannot be moved to an archived subject.", field="subject_id"
                    )
                changes["subject_id"] = subject_id
            if not changes:
                uow.rollback()
                return current
            updated = repo.update_assignment(actor.owner_id, assignment_id, changes)
            assert updated is not None
            self._audit(
                uow,
                actor,
                "assignment.update",
                "assignment",
                assignment_id,
                request_id,
                details={"fields": sorted(changes)},
            )
            uow.commit()
        return updated

    def delete_assignment(
        self,
        actor: ActorContext,
        assignment_id: UUID,
        *,
        request_id: str | UUID | None = None,
        at: datetime | None = None,
    ) -> Assignment:
        """Soft-delete an assignment, preserving audit and source lifecycle (7.14)."""
        self._require_owner(actor)
        moment = _now(self._clock, at)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            current = repo.get_assignment(actor.owner_id, assignment_id)
            if current is None:
                raise NotFoundError("The requested assignment was not found.")
            deleted = repo.soft_delete_assignment(actor.owner_id, assignment_id, moment)
            assert deleted is not None
            # Audit and the brief Source_Record are intentionally retained; only
            # the assignment leaves active work views.
            self._audit(
                uow,
                actor,
                "assignment.delete",
                "assignment",
                assignment_id,
                request_id,
                details={"brief_source_id": str(current.brief_source_id) if current.brief_source_id else None},
            )
            uow.commit()
        return deleted

    def record_effort(
        self,
        actor: ActorContext,
        *,
        minutes: int,
        assignment_id: UUID | None = None,
        study_block_id: UUID | None = None,
        request_id: str | UUID | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record actual effort against an assignment or study block (7.7)."""
        self._require_owner(actor)
        if assignment_id is None and study_block_id is None:
            raise ValidationError(
                "Effort must reference an assignment or study block.", field="assignment_id"
            )
        clean_minutes = validate_positive_minutes(minutes)
        moment = _now(self._clock, at)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            if assignment_id is not None:
                assignment = repo.get_assignment(actor.owner_id, assignment_id)
                if assignment is None:
                    raise NotFoundError("The requested assignment was not found.")
            entry = EffortEntry(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                minutes=clean_minutes,
                recorded_at=moment,
                assignment_id=assignment_id,
                study_block_id=study_block_id,
            )
            stored = repo.add_effort_entry(entry)
            self._audit(
                uow,
                actor,
                "assignment.record_effort",
                "effort_entry",
                stored.id,
                request_id,
                details={"minutes": clean_minutes},
            )
            uow.commit()

    # ------------------------------------------------------------------
    # Task breakdown drafts (Requirements 7.8, 7.9, 7.10, 7.11)
    # ------------------------------------------------------------------
    def draft_task_breakdown(
        self,
        actor: ActorContext,
        assignment_id: UUID,
        tasks: list[dict[str, Any]],
        *,
        provenance: dict[str, Any] | None = None,
    ) -> TaskBreakdownDraft:
        """Validate an editable draft without persisting it (7.8, 7.9).

        The returned draft stays entirely outside canonical state; no rows are
        written until :meth:`confirm_task_breakdown` is called.
        """
        self._require_owner(actor)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            assignment = repo.get_assignment(actor.owner_id, assignment_id)
            uow.rollback()
        if assignment is None:
            raise NotFoundError("The requested assignment was not found.")
        return normalize_draft(assignment_id, tasks, provenance=provenance)

    def confirm_task_breakdown(
        self,
        actor: ActorContext,
        assignment_id: UUID,
        tasks: list[dict[str, Any]],
        *,
        idempotency_key: str,
        provenance: dict[str, Any] | None = None,
        request_id: str | UUID | None = None,
    ) -> TaskConfirmation:
        """Persist a confirmed, edited task breakdown exactly once (7.10, 7.11)."""
        self._require_owner(actor)
        draft = normalize_draft(assignment_id, tasks, provenance=provenance)
        request_hash = hash_request(
            {
                "assignment_id": str(assignment_id),
                "tasks": [
                    {
                        "title": task.title,
                        "estimated_minutes": task.estimated_minutes,
                        "position": task.position,
                        "due_at": task.due_at,
                    }
                    for task in draft.tasks
                ],
            }
        )
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            assignment = repo.get_assignment(actor.owner_id, assignment_id)
            if assignment is None:
                raise NotFoundError("The requested assignment was not found.")

            claim = uow.idempotency.begin(
                owner_user_id=actor.owner_id,
                operation=_CONFIRM_TASKS_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.completed:
                existing = repo.list_tasks(actor.owner_id, assignment_id)
                uow.rollback()
                return TaskConfirmation(
                    assignment_id=assignment_id, tasks=tuple(existing), created=False
                )

            stored = repo.replace_tasks(actor.owner_id, assignment_id, list(draft.tasks))
            self._audit(
                uow,
                actor,
                "assignment_tasks.confirm",
                "assignment",
                assignment_id,
                request_id,
                details={"task_count": len(stored), "provenance": draft.provenance},
            )
            uow.idempotency.complete(
                owner_user_id=actor.owner_id,
                operation=_CONFIRM_TASKS_OPERATION,
                key=idempotency_key,
                response_status=201,
                result_ref=assignment_id,
            )
            uow.commit()
        return TaskConfirmation(assignment_id=assignment_id, tasks=tuple(stored), created=True)

    # ------------------------------------------------------------------
    # Goals (Requirement 7.13)
    # ------------------------------------------------------------------
    def create_goal(
        self,
        actor: ActorContext,
        *,
        title: str,
        subject_id: UUID | None = None,
        target_at: datetime | None = None,
        request_id: str | UUID | None = None,
    ) -> Goal:
        self._require_owner(actor)
        clean_title = validate_title(title)
        if target_at is not None:
            target_at = validate_future_or_any_datetime(target_at, field_name="target_at")
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            if subject_id is not None:
                subject = repo.get_subject(actor.owner_id, subject_id)
                if subject is None:
                    raise NotFoundError("The requested subject was not found.")
            goal = Goal(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                title=clean_title,
                subject_id=subject_id,
                target_at=target_at,
                status=GoalStatus.ACTIVE,
            )
            stored = repo.create_goal(goal)
            self._audit(uow, actor, "goal.create", "goal", stored.id, request_id)
            uow.commit()
        return stored

    def list_goals(self, actor: ActorContext) -> list[Goal]:
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            items = repo.list_goals(actor.owner_id)
            uow.rollback()
        return items

    def set_goal_status(
        self,
        actor: ActorContext,
        goal_id: UUID,
        status: GoalStatus,
        *,
        request_id: str | UUID | None = None,
    ) -> Goal:
        self._require_owner(actor)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            updated = repo.set_goal_status(actor.owner_id, goal_id, status)
            if updated is None:
                raise NotFoundError("The requested goal was not found.")
            self._audit(
                uow,
                actor,
                "goal.update",
                "goal",
                goal_id,
                request_id,
                details={"status": status.value},
            )
            uow.commit()
        return updated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _transition_assignment(
        self,
        actor: ActorContext,
        assignment_id: UUID,
        action: AssignmentAction,
        request_id: str | UUID | None,
    ) -> Assignment:
        self._require_owner(actor)
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            current = repo.get_assignment(actor.owner_id, assignment_id)
            if current is None:
                raise NotFoundError("The requested assignment was not found.")
            target = next_assignment_status(current.status, action)
            updated = repo.update_assignment(
                actor.owner_id, assignment_id, {"status": target.value}
            )
            assert updated is not None
            self._audit(
                uow,
                actor,
                f"assignment.{action.value}",
                "assignment",
                assignment_id,
                request_id,
                details={"from": current.status.value, "to": target.value},
            )
            uow.commit()
        return updated

    def _build_brief_source(
        self, owner_id: UUID, subject_id: UUID, brief: BriefIntake, at: datetime
    ) -> Source:
        if brief.method not in IntakeMethod:
            raise ValidationError("Unsupported brief intake method.", field="method")
        if brief.method in (IntakeMethod.MANUAL, IntakeMethod.PASTED):
            content = (brief.content or "").strip()
            if not content:
                raise ValidationError("Brief content is required for pasted intake.", field="content")
            checksum = _checksum(content)
            uri = brief.uri
        else:  # UPLOADED
            reference = (brief.uri or brief.content or "").strip()
            if not reference:
                raise ValidationError("An uploaded brief requires a file reference.", field="uri")
            checksum = _checksum(reference)
            uri = brief.uri
        provenance = {
            "intake_method": brief.method.value,
            "captured_at": at.astimezone(timezone.utc).isoformat(),
            "owner_user_id": str(owner_id),
        }
        return Source(
            id=uuid4(),
            owner_user_id=owner_id,
            subject_id=subject_id,
            kind=brief.kind,
            content_checksum=checksum,
            provenance=provenance,
            title=brief.title,
            uri=uri,
            status="active",
        )

    def _audit(
        self,
        uow: SqlUnitOfWork,
        actor: ActorContext,
        action: str,
        resource_kind: str,
        resource_id: UUID | None,
        request_id: str | UUID | None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        uow.audit.record(
            owner_user_id=actor.owner_id,
            actor_user_id=actor.actor_id,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            request_id=request_id,
            details=details,
        )

    def _unit_of_work(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._engine, self._clock)

    @staticmethod
    def _require_owner(actor: ActorContext) -> None:
        """Observers can never mutate learner data (Requirement 2.6)."""
        if not actor.is_owner:
            raise AuthorizationError("This action requires learner ownership.")
