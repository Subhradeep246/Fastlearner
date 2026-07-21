"""Learning repository port and SQLAlchemy Core implementation.

Covers the learning tables established earlier (``learning_events``,
``mastery_state``, ``bkt_parameter_sets``, ``review_state``,
``mastery_snapshots``, ``recommendations``) plus the read paths for the served
practice context (``question_versions``, ``concepts``, ``concept_edges``).

Ownership and served-context rules:

* Every learner-owned query carries the resolved owner id as a positional
  argument and never trusts a client-supplied owner (Requirements 2.10, 17.11).
* A learning event may only reference a served question version whose concept
  matches, so an out-of-context question or concept is rejected before any
  mastery update (Requirement 14.15).
* The contended mastery row is read with ``SELECT ... FOR UPDATE`` and carries a
  monotonic ``version`` so concurrent events serialize safely (Requirement
  14.2). ``with_for_update`` is a no-op on SQLite and locks on PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, and_, select

from app.domain.learning import BktParameters
from app.persistence.models import (
    bkt_parameter_sets,
    concept_edges,
    learning_events,
    mastery_snapshots,
    mastery_state,
    question_versions,
    recommendations,
    review_state,
    utc_datetime,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ServedQuestion:
    """The served practice context resolved for a learning event (14.15)."""

    question_version_id: UUID
    concept_id: UUID
    subject_id: UUID
    status: str


@dataclass(frozen=True)
class MasteryRow:
    """A persisted mastery record with its optimistic ``version`` (14.2)."""

    owner_user_id: UUID
    concept_id: UUID
    probability: Decimal
    parameter_set_id: UUID
    version: int


@dataclass(frozen=True)
class LearningEventRow:
    """A recorded learning event (Requirement 14.1)."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID
    concept_id: UUID
    question_version_id: UUID
    operation_scope: str
    idempotency_key: str
    correct: bool
    duration_ms: int
    hint_used: bool
    retry_count: int
    occurred_at: datetime


@dataclass(frozen=True)
class RecommendationRow:
    """A persisted recommendation (Requirements 14.10-14.13)."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID
    concept_id: UUID | None
    kind: str
    status: str
    rule_version: str
    evidence: dict[str, Any]
    reason: str


class LearningRepository(Protocol):
    """Port for learning-event, mastery, review, and recommendation persistence."""

    def get_served_question(self, question_version_id: UUID) -> ServedQuestion | None: ...

    def get_active_parameter_set(self) -> BktParameters | None: ...

    def get_parameter_set(self, parameter_set_id: UUID) -> BktParameters | None: ...

    def insert_learning_event(self, event: LearningEventRow) -> LearningEventRow: ...

    def get_learning_event(
        self, owner_user_id: UUID, event_id: UUID
    ) -> LearningEventRow | None: ...

    def lock_mastery(self, owner_user_id: UUID, concept_id: UUID) -> MasteryRow | None: ...

    def get_mastery(self, owner_user_id: UUID, concept_id: UUID) -> MasteryRow | None: ...

    def upsert_mastery(
        self,
        *,
        owner_user_id: UUID,
        concept_id: UUID,
        probability: Decimal,
        parameter_set_id: UUID,
        expected_version: int | None,
    ) -> MasteryRow: ...

    def insert_snapshot(
        self,
        *,
        id: UUID,
        owner_user_id: UUID,
        concept_id: UUID,
        learning_event_id: UUID,
        probability: Decimal,
        rule_version: str,
    ) -> None: ...

    def insert_recommendation(self, row: RecommendationRow) -> RecommendationRow: ...

    def get_recommendation_for_event(
        self, owner_user_id: UUID, concept_id: UUID, learning_event_id: UUID
    ) -> RecommendationRow | None: ...

    def successor_concept_ids(self, concept_id: UUID) -> list[UUID]: ...

    def prerequisite_concept_ids(self, concept_id: UUID) -> list[UUID]: ...

    def ensure_review_state(
        self,
        *,
        owner_user_id: UUID,
        concept_id: UUID,
        rule_version: str,
        interval_days: int,
        ease_factor: Decimal,
        due_at: datetime,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _parameters_from_row(row: Any) -> BktParameters:
    return BktParameters.create(
        key=row.key,
        version=row.version,
        prior=row.prior,
        transition=row.transition,
        slip=row.slip,
        guess=row.guess,
        id=row.id,
    )


def _event_from_row(row: Any) -> LearningEventRow:
    return LearningEventRow(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        concept_id=row.concept_id,
        question_version_id=row.question_version_id,
        operation_scope=row.operation_scope,
        idempotency_key=row.idempotency_key,
        correct=bool(row.correct),
        duration_ms=row.duration_ms,
        hint_used=bool(row.hint_used),
        retry_count=row.retry_count,
        occurred_at=_as_utc(row.occurred_at),  # type: ignore[arg-type]
    )


def _mastery_from_row(row: Any) -> MasteryRow:
    return MasteryRow(
        owner_user_id=row.owner_user_id,
        concept_id=row.concept_id,
        probability=Decimal(str(row.probability)),
        parameter_set_id=row.parameter_set_id,
        version=row.version,
    )


def _recommendation_from_row(row: Any) -> RecommendationRow:
    return RecommendationRow(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        concept_id=row.concept_id,
        kind=row.kind,
        status=row.status,
        rule_version=row.rule_version,
        evidence=dict(row.evidence or {}),
        reason=row.reason,
    )


class SqlLearningRepository(LearningRepository):
    """SQLAlchemy Core implementation backed by a live connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    # -- served practice context (Requirement 14.15) ----------------------
    def get_served_question(self, question_version_id: UUID) -> ServedQuestion | None:
        row = self._connection.execute(
            select(question_versions).where(question_versions.c.id == question_version_id)
        ).first()
        if row is None:
            return None
        return ServedQuestion(
            question_version_id=row.id,
            concept_id=row.concept_id,
            subject_id=row.subject_id,
            status=row.status,
        )

    # -- BKT parameter sets ------------------------------------------------
    def get_active_parameter_set(self) -> BktParameters | None:
        row = self._connection.execute(
            select(bkt_parameter_sets)
            .where(bkt_parameter_sets.c.status == "active")
            .order_by(bkt_parameter_sets.c.version.desc())
        ).first()
        return _parameters_from_row(row) if row is not None else None

    def get_parameter_set(self, parameter_set_id: UUID) -> BktParameters | None:
        row = self._connection.execute(
            select(bkt_parameter_sets).where(bkt_parameter_sets.c.id == parameter_set_id)
        ).first()
        return _parameters_from_row(row) if row is not None else None

    # -- learning events (Requirement 14.1) --------------------------------
    def insert_learning_event(self, event: LearningEventRow) -> LearningEventRow:
        self._connection.execute(
            learning_events.insert().values(
                id=event.id,
                owner_user_id=event.owner_user_id,
                subject_id=event.subject_id,
                concept_id=event.concept_id,
                question_version_id=event.question_version_id,
                operation_scope=event.operation_scope,
                idempotency_key=event.idempotency_key,
                correct=event.correct,
                duration_ms=event.duration_ms,
                hint_used=event.hint_used,
                retry_count=event.retry_count,
                occurred_at=utc_datetime(event.occurred_at),
            )
        )
        stored = self.get_learning_event(event.owner_user_id, event.id)
        assert stored is not None
        return stored

    def get_learning_event(
        self, owner_user_id: UUID, event_id: UUID
    ) -> LearningEventRow | None:
        row = self._connection.execute(
            select(learning_events).where(
                and_(
                    learning_events.c.id == event_id,
                    learning_events.c.owner_user_id == owner_user_id,
                )
            )
        ).first()
        return _event_from_row(row) if row is not None else None

    # -- mastery state (Requirement 14.2: lock/version) --------------------
    def lock_mastery(self, owner_user_id: UUID, concept_id: UUID) -> MasteryRow | None:
        row = self._connection.execute(
            select(mastery_state)
            .where(
                and_(
                    mastery_state.c.owner_user_id == owner_user_id,
                    mastery_state.c.concept_id == concept_id,
                )
            )
            .with_for_update()
        ).first()
        return _mastery_from_row(row) if row is not None else None

    def get_mastery(self, owner_user_id: UUID, concept_id: UUID) -> MasteryRow | None:
        row = self._connection.execute(
            select(mastery_state).where(
                and_(
                    mastery_state.c.owner_user_id == owner_user_id,
                    mastery_state.c.concept_id == concept_id,
                )
            )
        ).first()
        return _mastery_from_row(row) if row is not None else None

    def upsert_mastery(
        self,
        *,
        owner_user_id: UUID,
        concept_id: UUID,
        probability: Decimal,
        parameter_set_id: UUID,
        expected_version: int | None,
    ) -> MasteryRow:
        now = datetime.now(timezone.utc)
        if expected_version is None:
            self._connection.execute(
                mastery_state.insert().values(
                    owner_user_id=owner_user_id,
                    concept_id=concept_id,
                    probability=probability,
                    parameter_set_id=parameter_set_id,
                    version=1,
                )
            )
        else:
            result = self._connection.execute(
                mastery_state.update()
                .where(
                    and_(
                        mastery_state.c.owner_user_id == owner_user_id,
                        mastery_state.c.concept_id == concept_id,
                        mastery_state.c.version == expected_version,
                    )
                )
                .values(
                    probability=probability,
                    parameter_set_id=parameter_set_id,
                    version=expected_version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:  # pragma: no cover - concurrent conflict guard
                raise MasteryVersionConflict(concept_id)
        stored = self.get_mastery(owner_user_id, concept_id)
        assert stored is not None
        return stored

    # -- snapshots (Requirement 14.2) --------------------------------------
    def insert_snapshot(
        self,
        *,
        id: UUID,
        owner_user_id: UUID,
        concept_id: UUID,
        learning_event_id: UUID,
        probability: Decimal,
        rule_version: str,
    ) -> None:
        self._connection.execute(
            mastery_snapshots.insert().values(
                id=id,
                owner_user_id=owner_user_id,
                concept_id=concept_id,
                learning_event_id=learning_event_id,
                probability=probability,
                rule_version=rule_version,
            )
        )

    # -- recommendations (Requirements 14.10-14.13) ------------------------
    def insert_recommendation(self, row: RecommendationRow) -> RecommendationRow:
        self._connection.execute(
            recommendations.insert().values(
                id=row.id,
                owner_user_id=row.owner_user_id,
                subject_id=row.subject_id,
                concept_id=row.concept_id,
                kind=row.kind,
                status=row.status,
                rule_version=row.rule_version,
                evidence=dict(row.evidence),
                reason=row.reason,
            )
        )
        return row

    def get_recommendation_for_event(
        self, owner_user_id: UUID, concept_id: UUID, learning_event_id: UUID
    ) -> RecommendationRow | None:
        rows = self._connection.execute(
            select(recommendations)
            .where(
                and_(
                    recommendations.c.owner_user_id == owner_user_id,
                    recommendations.c.concept_id == concept_id,
                )
            )
            .order_by(recommendations.c.created_at.desc())
        ).all()
        target = str(learning_event_id)
        for row in rows:
            evidence = dict(row.evidence or {})
            if evidence.get("learning_event_id") == target:
                return _recommendation_from_row(row)
        return None

    # -- concept graph (Requirement 14.12) ---------------------------------
    def successor_concept_ids(self, concept_id: UUID) -> list[UUID]:
        rows = self._connection.execute(
            select(concept_edges.c.concept_id).where(
                concept_edges.c.prerequisite_concept_id == concept_id
            )
        ).all()
        return [row.concept_id for row in rows]

    def prerequisite_concept_ids(self, concept_id: UUID) -> list[UUID]:
        rows = self._connection.execute(
            select(concept_edges.c.prerequisite_concept_id).where(
                concept_edges.c.concept_id == concept_id
            )
        ).all()
        return [row.prerequisite_concept_id for row in rows]

    # -- first review scheduling (Requirement 14.12) -----------------------
    def ensure_review_state(
        self,
        *,
        owner_user_id: UUID,
        concept_id: UUID,
        rule_version: str,
        interval_days: int,
        ease_factor: Decimal,
        due_at: datetime,
    ) -> bool:
        existing = self._connection.execute(
            select(review_state.c.concept_id).where(
                and_(
                    review_state.c.owner_user_id == owner_user_id,
                    review_state.c.concept_id == concept_id,
                )
            )
        ).first()
        if existing is not None:
            return False
        self._connection.execute(
            review_state.insert().values(
                owner_user_id=owner_user_id,
                concept_id=concept_id,
                rule_version=rule_version,
                interval_days=interval_days,
                ease_factor=ease_factor,
                repetitions=0,
                due_at=utc_datetime(due_at),
            )
        )
        return True


class MasteryVersionConflict(RuntimeError):
    """Raised when an optimistic mastery version update finds no matching row."""

    def __init__(self, concept_id: UUID) -> None:
        super().__init__(f"Mastery version conflict for concept {concept_id}.")
        self.concept_id = concept_id
