"""Deterministic BKT, pacing, and recommendation rules (Requirement 14).

This module holds the pure value objects, enumerations, versioned rule
parameters, and total functions used by the Learning_Service. It has no
persistence, framework, or provider dependencies so the mastery rules stay
independently testable.

Key rules encoded here:

* Bayesian Knowledge Tracing is a total pure function for valid parameter sets.
  For a correct or incorrect observation it applies the posterior equations of
  Requirements 14.4 and 14.5, followed by the knowledge-transition step of
  Requirement 14.6 (``next = posterior + (1 - posterior) * P(T)``).
* Parameters satisfy ``0 <= p_* <= 1`` and denominators are validated nonzero;
  an invalid parameter set produces a typed configuration error rather than an
  update (Requirement 14 BKT design).
* Numeric precision plus a final defensive clamp keep every persisted
  probability in the inclusive ``[0, 1]`` range (Requirement 14.7).
* Correctness alone is the BKT observation (Requirement 14.8). Response
  duration, hint use, and retries only produce versioned pacing flags that are
  surfaced in the recommendation reason without altering the observation
  (Requirement 14.9).
* Recommendation bands are deterministic: below ``0.60`` recommends an alternate
  explanation/question for the same concept (14.10); ``[0.60, 0.85)`` continues
  the concept and inserts prerequisite/due review (14.11); ``>= 0.85`` marks the
  concept temporarily mastered, unlocks valid successors, and schedules a first
  review (14.12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID

from app.domain.identity import IdentityError

__all__ = [
    "BktConfigurationError",
    "BktParameters",
    "MASTERED_THRESHOLD",
    "DEVELOPING_THRESHOLD",
    "PACING_RULE_VERSION",
    "RECOMMENDATION_RULE_VERSION",
    "REVIEW_RULE_VERSION",
    "FIRST_REVIEW_INTERVAL_DAYS",
    "SLOW_RESPONSE_MS",
    "PacingFlag",
    "RecommendationBand",
    "RecommendationKind",
    "Recommendation",
    "bkt_posterior",
    "bkt_update",
    "clamp_probability",
    "derive_pacing_flags",
    "recommendation_band",
    "build_recommendation",
]


# ---------------------------------------------------------------------------
# Versioned rule constants
# ---------------------------------------------------------------------------

#: Recommendation band boundaries (Requirements 14.10, 14.11, 14.12).
DEVELOPING_THRESHOLD = Decimal("0.60")
MASTERED_THRESHOLD = Decimal("0.85")

#: Versioned rule identifiers surfaced in evidence and reasons (Requirement 14.13).
PACING_RULE_VERSION = "pacing-1"
RECOMMENDATION_RULE_VERSION = "recommendation-1"
REVIEW_RULE_VERSION = "review-sm2-1"

#: First-review scheduling for a newly mastered concept (Requirement 14.12).
FIRST_REVIEW_INTERVAL_DAYS = 1

#: Response time (milliseconds) above which a correct answer is flagged as slow
#: under the active pacing rule version (Requirement 14.9).
SLOW_RESPONSE_MS = 60_000

#: Probabilities persist as ``Numeric(8, 7)``; compute at the same precision.
_QUANTUM = Decimal("0.0000001")
_ZERO = Decimal(0)
_ONE = Decimal(1)


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class BktConfigurationError(IdentityError):
    """A BKT parameter set is invalid, so no mastery update is performed.

    Raised when a probability falls outside ``[0, 1]`` or a posterior
    denominator would be zero. The error is a configuration failure rather than
    an update, so canonical mastery is never changed (Requirement 14 BKT
    design).
    """

    code = "bkt_configuration_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class PacingFlag(StrEnum):
    """Versioned pacing evidence derived from non-correctness signals (14.9)."""

    SLOW_RESPONSE = "slow_response"
    HINT_USED = "hint_used"
    MULTIPLE_RETRIES = "multiple_retries"


class RecommendationBand(StrEnum):
    """The deterministic mastery band a probability falls into (14.10-14.12)."""

    BELOW = "below"  # P(L) < 0.60
    DEVELOPING = "developing"  # 0.60 <= P(L) < 0.85
    MASTERED = "mastered"  # P(L) >= 0.85


class RecommendationKind(StrEnum):
    """The learning action recommended for the current mastery band."""

    ALTERNATE_EXPLANATION = "alternate_explanation"
    CONTINUE_CONCEPT = "continue_concept"
    TEMPORARILY_MASTERED = "temporarily_mastered"


#: Maps each band to its recommended action kind.
_BAND_KIND: dict[RecommendationBand, RecommendationKind] = {
    RecommendationBand.BELOW: RecommendationKind.ALTERNATE_EXPLANATION,
    RecommendationBand.DEVELOPING: RecommendationKind.CONTINUE_CONCEPT,
    RecommendationBand.MASTERED: RecommendationKind.TEMPORARILY_MASTERED,
}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


def _as_probability(value: Any, name: str) -> Decimal:
    """Coerce ``value`` to a Decimal probability, validating the ``[0, 1]`` range."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise BktConfigurationError(f"{name} is not a valid probability.") from error
    if result.is_nan() or result < _ZERO or result > _ONE:
        raise BktConfigurationError(f"{name} must be within the inclusive range 0 to 1.")
    return result


@dataclass(frozen=True)
class BktParameters:
    """An effective versioned BKT parameter set (Requirement 14.6).

    ``prior`` is the initial knowledge probability ``P(L0)`` used when a concept
    has no mastery record yet. ``transition`` is ``P(T)``, ``slip`` is ``P(S)``,
    and ``guess`` is ``P(G)``. All four are validated to lie within ``[0, 1]``.
    """

    key: str
    version: int
    prior: Decimal
    transition: Decimal
    slip: Decimal
    guess: Decimal
    id: UUID | None = None

    @classmethod
    def create(
        cls,
        *,
        key: str,
        version: int,
        prior: Any,
        transition: Any,
        slip: Any,
        guess: Any,
        id: UUID | None = None,
    ) -> "BktParameters":
        """Validate and build a parameter set, raising on out-of-range values."""
        if not key or not key.strip():
            raise BktConfigurationError("A BKT parameter set requires a key.")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BktConfigurationError("A BKT parameter set requires a positive integer version.")
        return cls(
            key=key,
            version=version,
            prior=_as_probability(prior, "prior"),
            transition=_as_probability(transition, "transition"),
            slip=_as_probability(slip, "slip"),
            guess=_as_probability(guess, "guess"),
            id=id,
        )


@dataclass(frozen=True)
class Recommendation:
    """A deterministic, explainable recommendation (Requirements 14.10-14.13)."""

    kind: RecommendationKind
    band: RecommendationBand
    probability: Decimal
    rule_version: str
    reason: str
    evidence: Mapping[str, Any]
    next_action: str
    confidence_rationale: str
    pacing_flags: tuple[PacingFlag, ...] = ()
    unlocked_concept_ids: tuple[UUID, ...] = field(default_factory=tuple)
    schedules_first_review: bool = False


# ---------------------------------------------------------------------------
# Pure BKT functions (Requirements 14.4, 14.5, 14.6, 14.7)
# ---------------------------------------------------------------------------


def clamp_probability(value: Decimal) -> Decimal:
    """Defensively clamp and quantize a probability to ``[0, 1]`` (14.7)."""
    if value < _ZERO:
        value = _ZERO
    elif value > _ONE:
        value = _ONE
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def bkt_posterior(prior: Decimal, correct: bool, params: BktParameters) -> Decimal:
    """Return the observation posterior for a correct/incorrect answer.

    Correct (Requirement 14.4)::

        posterior = P(L)(1-P(S)) / (P(L)(1-P(S)) + (1-P(L))P(G))

    Incorrect (Requirement 14.5)::

        posterior = P(L)P(S) / (P(L)P(S) + (1-P(L))(1-P(G)))

    Denominators are validated nonzero; a zero denominator is a configuration
    error rather than a silent update.
    """
    prior = _as_probability(prior, "prior probability")
    slip = params.slip
    guess = params.guess
    if correct:
        numerator = prior * (_ONE - slip)
        denominator = prior * (_ONE - slip) + (_ONE - prior) * guess
    else:
        numerator = prior * slip
        denominator = prior * slip + (_ONE - prior) * (_ONE - guess)
    if denominator == _ZERO:
        raise BktConfigurationError(
            "BKT posterior denominator is zero for the given parameters and prior."
        )
    return numerator / denominator


def bkt_update(prior: Decimal, correct: bool, params: BktParameters) -> Decimal:
    """Return the next mastery probability after one observation (14.4-14.7).

    Applies the correctness posterior then the knowledge-transition step
    ``next = posterior + (1 - posterior) * P(T)`` and finally the defensive
    clamp to ``[0, 1]``.
    """
    posterior = bkt_posterior(prior, correct, params)
    next_probability = posterior + (_ONE - posterior) * params.transition
    return clamp_probability(next_probability)


# ---------------------------------------------------------------------------
# Pacing flags (Requirement 14.9) - never alter the correctness observation
# ---------------------------------------------------------------------------


def derive_pacing_flags(
    *, duration_ms: int, hint_used: bool, retry_count: int
) -> tuple[PacingFlag, ...]:
    """Derive versioned pacing flags from duration, hint use, and retries.

    These flags are surfaced in the recommendation reason only; the BKT update
    observes correctness alone (Requirements 14.8, 14.9).
    """
    flags: list[PacingFlag] = []
    if duration_ms is not None and duration_ms >= SLOW_RESPONSE_MS:
        flags.append(PacingFlag.SLOW_RESPONSE)
    if hint_used:
        flags.append(PacingFlag.HINT_USED)
    if retry_count and retry_count > 0:
        flags.append(PacingFlag.MULTIPLE_RETRIES)
    return tuple(flags)


# ---------------------------------------------------------------------------
# Recommendation bands (Requirements 14.10, 14.11, 14.12, 14.13)
# ---------------------------------------------------------------------------


def recommendation_band(probability: Decimal) -> RecommendationBand:
    """Classify a mastery probability into its deterministic band."""
    probability = _as_probability(probability, "mastery probability")
    if probability >= MASTERED_THRESHOLD:
        return RecommendationBand.MASTERED
    if probability >= DEVELOPING_THRESHOLD:
        return RecommendationBand.DEVELOPING
    return RecommendationBand.BELOW


_PACING_REASON = {
    PacingFlag.SLOW_RESPONSE: "a slower-than-usual response",
    PacingFlag.HINT_USED: "hint use",
    PacingFlag.MULTIPLE_RETRIES: "multiple retries",
}


def _pacing_reason_suffix(flags: tuple[PacingFlag, ...]) -> str:
    if not flags:
        return ""
    phrases = [_PACING_REASON[flag] for flag in flags]
    if len(phrases) == 1:
        joined = phrases[0]
    else:
        joined = ", ".join(phrases[:-1]) + f" and {phrases[-1]}"
    return f" Because of {joined}, a simpler explanation, a smaller block, or a prerequisite review is suggested."


def build_recommendation(
    *,
    probability: Decimal,
    prior_probability: Decimal,
    posterior: Decimal,
    correct: bool,
    pacing_flags: tuple[PacingFlag, ...],
    learning_event_id: UUID,
    question_version_id: UUID,
    concept_id: UUID,
    parameter_set_key: str,
    parameter_set_version: int,
    unlocked_concept_ids: tuple[UUID, ...] = (),
) -> Recommendation:
    """Assemble a deterministic, learner-readable recommendation (14.10-14.13).

    Every recommendation exposes its rule version, evidence identifiers, a
    confidence rationale, and a plain-language next action. Pacing evidence is
    included in the reason without changing the correctness-only observation
    (Requirement 14.9).
    """
    probability = clamp_probability(_as_probability(probability, "mastery probability"))
    band = recommendation_band(probability)
    kind = _BAND_KIND[band]
    schedules_first_review = band is RecommendationBand.MASTERED

    if band is RecommendationBand.BELOW:
        headline = (
            "Mastery is still below 60%, so try an alternate explanation or a "
            "different question for this same concept before moving on."
        )
        next_action = "Review an alternate explanation, then attempt another question for this concept."
    elif band is RecommendationBand.DEVELOPING:
        headline = (
            "Mastery is developing (60-85%). Continue with this concept and mix "
            "in a prerequisite or a due review to strengthen the foundation."
        )
        next_action = "Continue this concept and complete the inserted prerequisite or due review."
    else:
        headline = (
            "Mastery has reached at least 85%. This concept is temporarily "
            "mastered; its ready successors are unlocked and a first review is scheduled."
        )
        next_action = "Move on to an unlocked successor concept; the first spaced review is scheduled."

    reason = headline + _pacing_reason_suffix(pacing_flags)

    confidence_rationale = (
        f"Confidence follows the current mastery estimate of {probability} under "
        f"BKT parameter set '{parameter_set_key}' v{parameter_set_version}; the "
        f"last answer was {'correct' if correct else 'incorrect'}, moving the "
        f"estimate from {clamp_probability(_as_probability(prior_probability, 'prior'))} "
        f"to {probability}."
    )

    evidence: dict[str, Any] = {
        "learning_event_id": str(learning_event_id),
        "question_version_id": str(question_version_id),
        "concept_id": str(concept_id),
        "correct": correct,
        "prior_probability": str(clamp_probability(_as_probability(prior_probability, "prior"))),
        "posterior": str(clamp_probability(_as_probability(posterior, "posterior"))),
        "mastery_probability": str(probability),
        "band": band.value,
        "parameter_set_key": parameter_set_key,
        "parameter_set_version": parameter_set_version,
        "pacing_flags": [flag.value for flag in pacing_flags],
        "pacing_rule_version": PACING_RULE_VERSION,
        "recommendation_rule_version": RECOMMENDATION_RULE_VERSION,
    }
    if schedules_first_review:
        evidence["review_rule_version"] = REVIEW_RULE_VERSION
        evidence["first_review_interval_days"] = FIRST_REVIEW_INTERVAL_DAYS
    if unlocked_concept_ids:
        evidence["unlocked_concept_ids"] = [str(cid) for cid in unlocked_concept_ids]

    return Recommendation(
        kind=kind,
        band=band,
        probability=probability,
        rule_version=RECOMMENDATION_RULE_VERSION,
        reason=reason,
        evidence=evidence,
        next_action=next_action,
        confidence_rationale=confidence_rationale,
        pacing_flags=pacing_flags,
        unlocked_concept_ids=unlocked_concept_ids,
        schedules_first_review=schedules_first_review,
    )
