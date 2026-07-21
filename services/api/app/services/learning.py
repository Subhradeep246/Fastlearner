"""Transactional BKT learning events and explainable recommendations.

This application service orchestrates the single-transaction learning-event
workflow of Requirement 14. It composes the durable workflow primitives
established earlier: a unit of work for atomic commits, operation-scoped
idempotency records for duplicate-safe recording, an audit trail for confirmed
mutations, and the transactional outbox for follow-up aggregate work.

The workflow (Requirement 14.2) runs in one transaction:

1. Claim operation idempotency; a repeated key replays the prior outcome
   without a second mastery update, snapshot, or recommendation (14.3).
2. Validate the served question/concept/owner context (14.15).
3. Insert the learning event recording all evidence fields (14.1).
4. ``SELECT ... FOR UPDATE`` the mastery row and apply the effective versioned
   BKT parameter set (14.2, 14.6).
5. Update mastery and its version, clamped to ``[0, 1]`` (14.7).
6. Insert a mastery snapshot (14.2).
7. On the mastered band, unlock valid successors and schedule a first review
   (14.12); build the deterministic recommendation (14.10-14.13).
8. Enqueue aggregate-refresh outbox work and commit.

Any failure rolls back the event, mastery update, snapshot, and recommendation
as one unit (Requirement 14.14). Correctness alone is the BKT observation;
duration, hint, and retry evidence only inform pacing reasons (14.8, 14.9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine

from app.clock import Clock, system_clock
from app.domain.identity import (
    ActorContext,
    AuthorizationError,
    NotFoundError,
    ValidationError,
)
from app.domain.learning import (
    FIRST_REVIEW_INTERVAL_DAYS,
    REVIEW_RULE_VERSION,
    BktConfigurationError,
    BktParameters,
    Recommendation,
    bkt_posterior,
    bkt_update,
    build_recommendation,
    derive_pacing_flags,
)
from app.repositories.idempotency import hash_request
from app.repositories.learning import (
    LearningEventRow,
    LearningRepository,
    MasteryRow,
    RecommendationRow,
    SqlLearningRepository,
)
from app.repositories.unit_of_work import SqlUnitOfWork

RepositoryFactory = Callable[[Connection], LearningRepository]

_RECORD_EVENT_OPERATION = "learning.record_event"
_DEFAULT_EASE_FACTOR = Decimal("2.5")


@dataclass(frozen=True)
class LearningEventResult:
    """The outcome of recording a learning event.

    ``created`` is ``False`` when a repeated idempotency key replayed the
    original outcome without a second update (Requirement 14.3).
    """

    event: LearningEventRow
    mastery: MasteryRow
    recommendation: RecommendationRow
    created: bool


def _now(clock: Clock, at: datetime | None) -> datetime:
    return at if at is not None else clock()


def _validate_nonnegative_int(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer.", field=field)
    return value


class LearningService:
    """Use cases for transactional learning events and recommendations."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock = system_clock,
        repository_factory: RepositoryFactory = SqlLearningRepository,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._repository_factory = repository_factory

    def record_learning_event(
        self,
        actor: ActorContext,
        *,
        concept_id: UUID,
        question_version_id: UUID,
        correct: bool,
        duration_ms: int,
        hint_used: bool,
        retry_count: int,
        idempotency_key: str,
        occurred_at: datetime | None = None,
        request_id: str | UUID | None = None,
    ) -> LearningEventResult:
        """Record a learning event and produce a recommendation atomically (14.2)."""
        self._require_owner(actor)
        if not idempotency_key:
            raise ValidationError("An idempotency key is required.", field="idempotency_key")
        clean_duration = _validate_nonnegative_int(duration_ms, "duration_ms")
        clean_retries = _validate_nonnegative_int(retry_count, "retry_count")
        moment = _now(self._clock, occurred_at)

        request_hash = hash_request(
            {
                "concept_id": str(concept_id),
                "question_version_id": str(question_version_id),
                "correct": bool(correct),
                "duration_ms": clean_duration,
                "hint_used": bool(hint_used),
                "retry_count": clean_retries,
            }
        )

        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)

            # 1. Claim operation idempotency (Requirement 14.3).
            claim = uow.idempotency.begin(
                owner_user_id=actor.owner_id,
                operation=_RECORD_EVENT_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.completed and claim.outcome and claim.outcome.result_ref is not None:
                replay = self._replay(repo, actor.owner_id, claim.outcome.result_ref)
                uow.rollback()
                return replay

            # 2. Validate served question/concept/owner context (14.15).
            served = repo.get_served_question(question_version_id)
            if served is None:
                raise NotFoundError("The referenced question version was not found.")
            if served.concept_id != concept_id:
                raise ValidationError(
                    "The question version does not belong to the referenced concept.",
                    field="concept_id",
                )
            subject_id = served.subject_id

            # 3. Insert the learning event (Requirement 14.1).
            event = repo.insert_learning_event(
                LearningEventRow(
                    id=uuid4(),
                    owner_user_id=actor.owner_id,
                    subject_id=subject_id,
                    concept_id=concept_id,
                    question_version_id=question_version_id,
                    operation_scope=_RECORD_EVENT_OPERATION,
                    idempotency_key=idempotency_key,
                    correct=bool(correct),
                    duration_ms=clean_duration,
                    hint_used=bool(hint_used),
                    retry_count=clean_retries,
                    occurred_at=moment,
                )
            )

            # 4. Lock the mastery row and resolve the effective parameter set.
            mastery = repo.lock_mastery(actor.owner_id, concept_id)
            params = self._effective_parameters(repo, mastery)
            prior = mastery.probability if mastery is not None else params.prior

            # 5. Apply BKT with correctness as the only observation (14.4-14.8).
            posterior = bkt_posterior(prior, bool(correct), params)
            next_probability = bkt_update(prior, bool(correct), params)

            assert params.id is not None
            updated_mastery = repo.upsert_mastery(
                owner_user_id=actor.owner_id,
                concept_id=concept_id,
                probability=next_probability,
                parameter_set_id=params.id,
                expected_version=None if mastery is None else mastery.version,
            )

            # 6. Insert a mastery snapshot (Requirement 14.2).
            repo.insert_snapshot(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                concept_id=concept_id,
                learning_event_id=event.id,
                probability=next_probability,
                rule_version=params.key + f":v{params.version}",
            )

            # 7. Pacing evidence (14.9) and successor unlock / first review (14.12).
            pacing_flags = derive_pacing_flags(
                duration_ms=clean_duration, hint_used=bool(hint_used), retry_count=clean_retries
            )
            unlocked = self._unlock_successors(
                repo, actor.owner_id, concept_id, next_probability, params
            )
            recommendation = build_recommendation(
                probability=next_probability,
                prior_probability=prior,
                posterior=posterior,
                correct=bool(correct),
                pacing_flags=pacing_flags,
                learning_event_id=event.id,
                question_version_id=question_version_id,
                concept_id=concept_id,
                parameter_set_key=params.key,
                parameter_set_version=params.version,
                unlocked_concept_ids=unlocked,
            )
            if recommendation.schedules_first_review:
                repo.ensure_review_state(
                    owner_user_id=actor.owner_id,
                    concept_id=concept_id,
                    rule_version=REVIEW_RULE_VERSION,
                    interval_days=FIRST_REVIEW_INTERVAL_DAYS,
                    ease_factor=_DEFAULT_EASE_FACTOR,
                    due_at=moment + timedelta(days=FIRST_REVIEW_INTERVAL_DAYS),
                )

            stored_recommendation = repo.insert_recommendation(
                self._recommendation_row(actor.owner_id, subject_id, concept_id, recommendation)
            )

            # 8. Durable follow-up work and audit, then complete idempotency.
            uow.outbox.enqueue(
                owner_user_id=actor.owner_id,
                kind="analytics.refresh_aggregates",
                deduplication_key=f"learning_event:{event.id}",
                payload={
                    "owner_user_id": str(actor.owner_id),
                    "subject_id": str(subject_id),
                    "concept_id": str(concept_id),
                    "learning_event_id": str(event.id),
                },
            )
            uow.audit.record(
                owner_user_id=actor.owner_id,
                actor_user_id=actor.actor_id,
                action="learning.record_event",
                resource_kind="learning_event",
                resource_id=event.id,
                request_id=request_id,
                details={
                    "concept_id": str(concept_id),
                    "correct": bool(correct),
                    "mastery_probability": str(next_probability),
                    "band": recommendation.band.value,
                },
            )
            uow.idempotency.complete(
                owner_user_id=actor.owner_id,
                operation=_RECORD_EVENT_OPERATION,
                key=idempotency_key,
                response_status=201,
                result_ref=event.id,
            )
            uow.commit()

        return LearningEventResult(
            event=event,
            mastery=updated_mastery,
            recommendation=stored_recommendation,
            created=True,
        )

    def get_mastery(self, actor: ActorContext, concept_id: UUID) -> MasteryRow | None:
        """Read the current mastery record for a concept within owner scope."""
        with self._unit_of_work() as uow:
            repo = self._repository_factory(uow.connection)
            mastery = repo.get_mastery(actor.owner_id, concept_id)
            uow.rollback()
        return mastery

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _replay(
        self, repo: LearningRepository, owner_id: UUID, event_id: UUID
    ) -> LearningEventResult:
        event = repo.get_learning_event(owner_id, event_id)
        if event is None:  # pragma: no cover - defensive
            raise NotFoundError("The original learning event was not found.")
        mastery = repo.get_mastery(owner_id, event.concept_id)
        recommendation = repo.get_recommendation_for_event(
            owner_id, event.concept_id, event_id
        )
        if mastery is None or recommendation is None:  # pragma: no cover - defensive
            raise NotFoundError("The original learning outcome was not found.")
        return LearningEventResult(
            event=event, mastery=mastery, recommendation=recommendation, created=False
        )

    def _effective_parameters(
        self, repo: LearningRepository, mastery: MasteryRow | None
    ) -> BktParameters:
        """Resolve the effective versioned BKT parameter set for the update."""
        if mastery is not None:
            params = repo.get_parameter_set(mastery.parameter_set_id)
            if params is None:  # pragma: no cover - defensive
                raise BktConfigurationError("The mastery parameter set is unavailable.")
            return params
        params = repo.get_active_parameter_set()
        if params is None:
            raise BktConfigurationError("No active BKT parameter set is configured.")
        return params

    def _unlock_successors(
        self,
        repo: LearningRepository,
        owner_id: UUID,
        concept_id: UUID,
        probability: Decimal,
        params: BktParameters,
    ) -> tuple[UUID, ...]:
        """Unlock successors whose prerequisites are all mastered (14.12).

        A concept reaching the mastered band unlocks each successor for which
        every prerequisite is now mastered. Unlocking is represented by ensuring
        an initial mastery record exists so the successor becomes servable.
        """
        from app.domain.learning import recommendation_band, RecommendationBand

        if recommendation_band(probability) is not RecommendationBand.MASTERED:
            return ()
        assert params.id is not None
        unlocked: list[UUID] = []
        for successor_id in repo.successor_concept_ids(concept_id):
            if repo.get_mastery(owner_id, successor_id) is not None:
                continue
            prerequisites = repo.prerequisite_concept_ids(successor_id)
            if self._all_mastered(repo, owner_id, prerequisites, concept_id, probability):
                repo.upsert_mastery(
                    owner_user_id=owner_id,
                    concept_id=successor_id,
                    probability=params.prior,
                    parameter_set_id=params.id,
                    expected_version=None,
                )
                unlocked.append(successor_id)
        return tuple(unlocked)

    @staticmethod
    def _all_mastered(
        repo: LearningRepository,
        owner_id: UUID,
        prerequisites: list[UUID],
        just_mastered_id: UUID,
        just_mastered_probability: Decimal,
    ) -> bool:
        from app.domain.learning import MASTERED_THRESHOLD

        for prerequisite_id in prerequisites:
            if prerequisite_id == just_mastered_id:
                if just_mastered_probability < MASTERED_THRESHOLD:
                    return False
                continue
            state = repo.get_mastery(owner_id, prerequisite_id)
            if state is None or state.probability < MASTERED_THRESHOLD:
                return False
        return True

    @staticmethod
    def _recommendation_row(
        owner_id: UUID,
        subject_id: UUID,
        concept_id: UUID,
        recommendation: Recommendation,
    ) -> RecommendationRow:
        return RecommendationRow(
            id=uuid4(),
            owner_user_id=owner_id,
            subject_id=subject_id,
            concept_id=concept_id,
            kind=recommendation.kind.value,
            status="active",
            rule_version=recommendation.rule_version,
            evidence=dict(recommendation.evidence),
            reason=recommendation.reason,
        )

    def _unit_of_work(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._engine, self._clock)

    @staticmethod
    def _require_owner(actor: ActorContext) -> None:
        """Observers can never mutate learner mastery (Requirements 2.6, 14.15)."""
        if not actor.is_owner:
            raise AuthorizationError("Recording a learning event requires learner ownership.")
