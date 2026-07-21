"""Unit and integration tests for transactional BKT learning events.

Covers Requirement 14: the pure BKT posterior/transition equations and bounds
(14.4-14.8), pacing evidence that never alters the observation (14.9),
recommendation bands (14.10-14.12), explainable recommendation output (14.13),
the single-transaction learning-event workflow with locking/versioning,
snapshots, successor unlock, and first-review scheduling (14.1, 14.2, 14.12),
idempotent replay (14.3), atomic rollback (14.14), and served-context
validation (14.15).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.domain.identity import (
    LEARNER_OWNER_SCOPES,
    ActorContext,
    AuthorizationError,
    NotFoundError,
    Role,
    ValidationError,
)
from app.domain.learning import (
    DEVELOPING_THRESHOLD,
    MASTERED_THRESHOLD,
    BktConfigurationError,
    BktParameters,
    PacingFlag,
    RecommendationBand,
    RecommendationKind,
    bkt_posterior,
    bkt_update,
    build_recommendation,
    clamp_probability,
    derive_pacing_flags,
    recommendation_band,
)
from app.persistence.curriculum_pack import _concept_id, _uuid, mathematics_manifest
from app.persistence.models import (
    learning_events,
    mastery_snapshots,
    mastery_state,
    recommendations,
    review_state,
)
from app.persistence.seeds import (
    DEFAULT_BKT_PARAMETER_SET_ID,
    LOCAL_LEARNER_ID,
    LOCAL_PARENT_ID,
    apply_curriculum_manifest,
    seed_default_bkt_parameters,
    seed_local_personas,
)
from app.repositories.learning import SqlLearningRepository
from app.services.learning import LearningService

FIXED_NOW = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)

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
    scopes=frozenset({"learning:read"}),
)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from app.persistence.models import metadata

    metadata.create_all(engine)
    with engine.begin() as connection:
        seed_local_personas(connection)
        seed_default_bkt_parameters(connection)
        apply_curriculum_manifest(connection, mathematics_manifest())
    return engine


def _service(engine) -> LearningService:
    return LearningService(engine, clock=FixedClock(FIXED_NOW))


def _concept(key: str) -> UUID:
    return UUID(_concept_id(key))


def _question(key: str, index: int = 1) -> UUID:
    return UUID(_uuid("question", f"{key}_q{index}", "1"))


ROOT_KEY = "whole_numbers_and_place_value"


def _default_params(**overrides) -> BktParameters:
    values = {
        "key": "default",
        "version": 1,
        "prior": "0.3",
        "transition": "0.1",
        "slip": "0.1",
        "guess": "0.2",
    }
    values.update(overrides)
    return BktParameters.create(**values)


# ---------------------------------------------------------------------------
# Pure BKT equations and bounds (Requirements 14.4, 14.5, 14.6, 14.7)
# ---------------------------------------------------------------------------


def test_correct_posterior_matches_specified_equation() -> None:
    params = _default_params()
    prior = Decimal("0.3")
    # L(1-S) / (L(1-S) + (1-L)G) = 0.27 / (0.27 + 0.14)
    expected = (prior * Decimal("0.9")) / (prior * Decimal("0.9") + Decimal("0.7") * Decimal("0.2"))
    assert bkt_posterior(prior, True, params) == expected


def test_incorrect_posterior_matches_specified_equation() -> None:
    params = _default_params()
    prior = Decimal("0.3")
    # L*S / (L*S + (1-L)(1-G))
    expected = (prior * Decimal("0.1")) / (prior * Decimal("0.1") + Decimal("0.7") * Decimal("0.8"))
    assert bkt_posterior(prior, False, params) == expected


def test_bkt_update_applies_transition_and_clamps() -> None:
    params = _default_params()
    prior = Decimal("0.3")
    posterior = bkt_posterior(prior, True, params)
    expected = clamp_probability(posterior + (Decimal(1) - posterior) * Decimal("0.1"))
    assert bkt_update(prior, True, params) == expected
    assert Decimal(0) <= bkt_update(prior, True, params) <= Decimal(1)


def test_correct_answer_increases_and_incorrect_decreases() -> None:
    params = _default_params()
    prior = Decimal("0.5")
    assert bkt_update(prior, True, params) > prior
    assert bkt_update(prior, False, params) < prior


def test_probabilities_stay_within_bounds_at_extremes() -> None:
    params = _default_params(prior="1", slip="0", guess="0")
    assert bkt_update(Decimal("1"), True, params) <= Decimal(1)
    assert bkt_update(Decimal("0"), False, params) >= Decimal(0)


def test_invalid_parameters_raise_configuration_error() -> None:
    with pytest.raises(BktConfigurationError):
        BktParameters.create(key="bad", version=1, prior="1.5", transition="0.1", slip="0.1", guess="0.2")
    with pytest.raises(BktConfigurationError):
        BktParameters.create(key="bad", version=1, prior="0.3", transition="-0.1", slip="0.1", guess="0.2")


def test_zero_denominator_is_configuration_error() -> None:
    # prior=0 and guess=0 make the correct-answer denominator zero.
    params = _default_params(prior="0.3", guess="0")
    with pytest.raises(BktConfigurationError):
        bkt_posterior(Decimal("0"), True, params)


# ---------------------------------------------------------------------------
# Recommendation bands and pacing (Requirements 14.9, 14.10, 14.11, 14.12)
# ---------------------------------------------------------------------------


def test_recommendation_bands_are_deterministic() -> None:
    assert recommendation_band(Decimal("0.59")) is RecommendationBand.BELOW
    assert recommendation_band(DEVELOPING_THRESHOLD) is RecommendationBand.DEVELOPING
    assert recommendation_band(Decimal("0.84")) is RecommendationBand.DEVELOPING
    assert recommendation_band(MASTERED_THRESHOLD) is RecommendationBand.MASTERED
    assert recommendation_band(Decimal("0.99")) is RecommendationBand.MASTERED


def test_pacing_flags_reflect_evidence_without_changing_observation() -> None:
    assert derive_pacing_flags(duration_ms=10, hint_used=False, retry_count=0) == ()
    flags = derive_pacing_flags(duration_ms=120_000, hint_used=True, retry_count=2)
    assert set(flags) == {PacingFlag.SLOW_RESPONSE, PacingFlag.HINT_USED, PacingFlag.MULTIPLE_RETRIES}


def test_recommendation_exposes_rule_evidence_and_next_action() -> None:
    event_id, question_id, concept_id = uuid4(), uuid4(), uuid4()
    recommendation = build_recommendation(
        probability=Decimal("0.5"),
        prior_probability=Decimal("0.3"),
        posterior=Decimal("0.45"),
        correct=True,
        pacing_flags=(PacingFlag.HINT_USED,),
        learning_event_id=event_id,
        question_version_id=question_id,
        concept_id=concept_id,
        parameter_set_key="default",
        parameter_set_version=1,
    )
    assert recommendation.kind is RecommendationKind.ALTERNATE_EXPLANATION
    assert recommendation.rule_version
    assert recommendation.evidence["learning_event_id"] == str(event_id)
    assert recommendation.evidence["question_version_id"] == str(question_id)
    assert "hint use" in recommendation.reason
    assert recommendation.next_action
    assert recommendation.confidence_rationale


def test_mastered_recommendation_schedules_first_review() -> None:
    recommendation = build_recommendation(
        probability=Decimal("0.9"),
        prior_probability=Decimal("0.8"),
        posterior=Decimal("0.88"),
        correct=True,
        pacing_flags=(),
        learning_event_id=uuid4(),
        question_version_id=uuid4(),
        concept_id=uuid4(),
        parameter_set_key="default",
        parameter_set_version=1,
    )
    assert recommendation.band is RecommendationBand.MASTERED
    assert recommendation.schedules_first_review is True


# ---------------------------------------------------------------------------
# Transactional workflow (Requirements 14.1, 14.2, 14.13)
# ---------------------------------------------------------------------------


def _record(service, concept_key=ROOT_KEY, *, correct=True, key="event-1", **overrides):
    kwargs = dict(
        concept_id=_concept(concept_key),
        question_version_id=_question(concept_key),
        correct=correct,
        duration_ms=5_000,
        hint_used=False,
        retry_count=0,
        idempotency_key=key,
    )
    kwargs.update(overrides)
    return service.record_learning_event(LEARNER, **kwargs)


def test_record_event_persists_event_mastery_snapshot_and_recommendation() -> None:
    engine = _engine()
    service = _service(engine)
    result = _record(service)

    assert result.created is True
    assert result.event.owner_user_id == LOCAL_LEARNER_ID
    assert Decimal(0) <= result.mastery.probability <= Decimal(1)
    assert result.recommendation.reason
    assert result.recommendation.evidence["learning_event_id"] == str(result.event.id)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(learning_events)) == 1
        assert connection.scalar(select(func.count()).select_from(mastery_snapshots)) == 1
        assert connection.scalar(select(func.count()).select_from(recommendations)) == 1
        mastery_row = connection.execute(select(mastery_state)).mappings().one()
        assert mastery_row["parameter_set_id"] == DEFAULT_BKT_PARAMETER_SET_ID
        assert mastery_row["version"] == 1


def test_repeated_events_increment_mastery_version() -> None:
    engine = _engine()
    service = _service(engine)
    _record(service, key="event-1")
    _record(service, key="event-2")
    with engine.connect() as connection:
        mastery_row = connection.execute(
            select(mastery_state).where(mastery_state.c.concept_id == _concept(ROOT_KEY))
        ).mappings().one()
        assert mastery_row["version"] == 2
        assert connection.scalar(select(func.count()).select_from(learning_events)) == 2


def test_correct_event_from_default_prior_is_developing_band() -> None:
    service = _service(_engine())
    result = _record(service, correct=True)
    # prior 0.3 -> ~0.69, which is the developing band (0.60-0.85).
    assert result.recommendation.evidence["band"] == RecommendationBand.DEVELOPING.value
    assert result.recommendation.kind == RecommendationKind.CONTINUE_CONCEPT.value


# ---------------------------------------------------------------------------
# Idempotent replay (Requirement 14.3)
# ---------------------------------------------------------------------------


def test_repeated_idempotency_key_replays_prior_outcome() -> None:
    engine = _engine()
    service = _service(engine)
    first = _record(service, key="dup")
    replay = _record(service, key="dup")

    assert replay.created is False
    assert replay.event.id == first.event.id
    assert replay.recommendation.id == first.recommendation.id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(learning_events)) == 1
        assert connection.scalar(select(func.count()).select_from(mastery_snapshots)) == 1
        assert connection.scalar(select(func.count()).select_from(recommendations)) == 1


# ---------------------------------------------------------------------------
# Served-context validation (Requirement 14.15)
# ---------------------------------------------------------------------------


def test_unknown_question_version_is_not_found() -> None:
    service = _service(_engine())
    with pytest.raises(NotFoundError):
        service.record_learning_event(
            LEARNER,
            concept_id=_concept(ROOT_KEY),
            question_version_id=uuid4(),
            correct=True,
            duration_ms=1000,
            hint_used=False,
            retry_count=0,
            idempotency_key="x",
        )


def test_concept_mismatch_is_validation_error_without_mutation() -> None:
    engine = _engine()
    service = _service(engine)
    with pytest.raises(ValidationError):
        service.record_learning_event(
            LEARNER,
            concept_id=_concept("division_as_sharing"),
            question_version_id=_question(ROOT_KEY),  # belongs to a different concept
            correct=True,
            duration_ms=1000,
            hint_used=False,
            retry_count=0,
            idempotency_key="x",
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(learning_events)) == 0
        assert connection.scalar(select(func.count()).select_from(mastery_state)) == 0


# ---------------------------------------------------------------------------
# Successor unlock and first review at mastery (Requirement 14.12)
# ---------------------------------------------------------------------------


def _seed_mastery(engine, concept_key: str, probability: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            mastery_state.insert().values(
                owner_user_id=LOCAL_LEARNER_ID,
                concept_id=_concept(concept_key),
                probability=probability,
                parameter_set_id=DEFAULT_BKT_PARAMETER_SET_ID,
                version=1,
            )
        )


def test_reaching_mastery_unlocks_successors_and_schedules_review() -> None:
    engine = _engine()
    service = _service(engine)
    # Start near mastery so a single correct answer crosses 0.85.
    _seed_mastery(engine, ROOT_KEY, "0.8")

    result = _record(service, correct=True)
    assert result.mastery.probability >= MASTERED_THRESHOLD
    assert result.recommendation.evidence["band"] == RecommendationBand.MASTERED.value

    unlocked = {UUID(cid) for cid in result.recommendation.evidence.get("unlocked_concept_ids", [])}
    # whole_numbers_and_place_value is the sole prerequisite of these two.
    assert _concept("division_as_sharing") in unlocked
    assert _concept("decimal_place_value") in unlocked

    with engine.connect() as connection:
        review_count = connection.scalar(
            select(func.count()).select_from(review_state).where(
                review_state.c.concept_id == _concept(ROOT_KEY)
            )
        )
        assert review_count == 1
        # Unlocked successors have an initial mastery record so they are servable.
        unlocked_states = connection.scalar(
            select(func.count()).select_from(mastery_state).where(
                mastery_state.c.concept_id.in_([
                    _concept("division_as_sharing"),
                    _concept("decimal_place_value"),
                ])
            )
        )
        assert unlocked_states == 2


# ---------------------------------------------------------------------------
# Atomic rollback and authorization (Requirements 14.14, 14.15, 2.6)
# ---------------------------------------------------------------------------


class _FailingRepository(SqlLearningRepository):
    """Fails when inserting the recommendation to force a rollback."""

    def insert_recommendation(self, row):  # type: ignore[override]
        raise RuntimeError("boom")


def test_failure_rolls_back_the_entire_transaction() -> None:
    engine = _engine()
    service = LearningService(
        engine, clock=FixedClock(FIXED_NOW), repository_factory=_FailingRepository
    )
    with pytest.raises(RuntimeError):
        _record(service)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(learning_events)) == 0
        assert connection.scalar(select(func.count()).select_from(mastery_state)) == 0
        assert connection.scalar(select(func.count()).select_from(mastery_snapshots)) == 0
        assert connection.scalar(select(func.count()).select_from(recommendations)) == 0


def test_observer_cannot_record_learning_event() -> None:
    service = _service(_engine())
    with pytest.raises(AuthorizationError):
        service.record_learning_event(
            OBSERVER,
            concept_id=_concept(ROOT_KEY),
            question_version_id=_question(ROOT_KEY),
            correct=True,
            duration_ms=1000,
            hint_used=False,
            retry_count=0,
            idempotency_key="x",
        )
