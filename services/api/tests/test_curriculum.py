"""Tests for the curriculum DAG, content lifecycle, and mathematics seed pack.

Covers Requirement 12: acyclic publication validation with cycle-edge reporting
(12.2, 12.23), the exact 15-concept prerequisite graph (12.3-12.18), the
four-state content lifecycle and review gating (12.20-12.22), retired-version
exclusion (12.24), and idempotent seeding of one lesson plus three questions per
concept (12.19).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.domain.curriculum import (
    ConceptEdge,
    ContentAction,
    ContentItem,
    ContentKind,
    ContentState,
    CyclicPrerequisiteError,
    QuestionVersion,
    ReviewDecision,
    next_content_state,
    record_reviewer_decision,
    select_servable_versions,
    topological_order,
    validate_acyclic,
)
from app.domain.identity import ValidationError
from app.persistence.curriculum_pack import (
    REVIEWER_ID,
    _CONCEPTS,
    _EDGES,
    _concept_id,
    mathematics_manifest,
)
from app.persistence.models import (
    concept_edges,
    concepts,
    content_items,
    content_reviews,
    curriculum_seed_manifests,
    question_versions,
    subjects,
    users,
)
from app.persistence.seeds import apply_curriculum_manifest


# ---------------------------------------------------------------------------
# Acyclic validation and cycle-edge reporting (12.2, 12.23)
# ---------------------------------------------------------------------------


def test_topological_order_places_prerequisites_first() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    edges = [ConceptEdge(concept_id=b, prerequisite_concept_id=a), ConceptEdge(concept_id=c, prerequisite_concept_id=b)]
    order = topological_order([a, b, c], edges)
    assert order.index(a) < order.index(b) < order.index(c)


def test_validate_acyclic_reports_the_cycle_edges() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    edges = [
        ConceptEdge(concept_id=b, prerequisite_concept_id=a),
        ConceptEdge(concept_id=c, prerequisite_concept_id=b),
        ConceptEdge(concept_id=a, prerequisite_concept_id=c),
    ]
    with pytest.raises(CyclicPrerequisiteError) as caught:
        validate_acyclic([a, b, c], edges)
    involved = {(edge.concept_id, edge.prerequisite_concept_id) for edge in caught.value.cycle_edges}
    assert involved == {(b, a), (c, b), (a, c)}
    assert caught.value.safe_payload()["code"] == "curriculum_cycle_detected"
    assert len(caught.value.safe_payload()["cycle_edges"]) == 3


def test_self_edge_is_reported_as_a_cycle() -> None:
    a = uuid4()
    with pytest.raises(CyclicPrerequisiteError) as caught:
        validate_acyclic([a], [ConceptEdge(concept_id=a, prerequisite_concept_id=a)])
    assert caught.value.cycle_edges[0].concept_id == a


def test_edge_referencing_unknown_concept_is_a_validation_error() -> None:
    a, b = uuid4(), uuid4()
    with pytest.raises(ValidationError):
        topological_order([a], [ConceptEdge(concept_id=a, prerequisite_concept_id=b)])


# ---------------------------------------------------------------------------
# Content lifecycle transitions and review gating (12.20, 12.21, 12.22)
# ---------------------------------------------------------------------------


def _approval() -> object:
    return record_reviewer_decision(
        reviewer_user_id=uuid4(),
        decision=ReviewDecision.APPROVED,
        version=1,
        reviewed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source="curated",
    )


def test_publish_requires_an_approving_review() -> None:
    reviewed = next_content_state(ContentState.DRAFT, ContentAction.APPROVE)
    assert reviewed is ContentState.REVIEWED
    # Publishing without an approval is rejected (12.21).
    with pytest.raises(Exception):
        next_content_state(ContentState.REVIEWED, ContentAction.PUBLISH)
    published = next_content_state(ContentState.REVIEWED, ContentAction.PUBLISH, approving_review=_approval())
    assert published is ContentState.PUBLISHED


def test_retire_only_applies_to_published_content() -> None:
    assert next_content_state(ContentState.PUBLISHED, ContentAction.RETIRE) is ContentState.RETIRED
    with pytest.raises(Exception):
        next_content_state(ContentState.DRAFT, ContentAction.RETIRE)


def test_record_reviewer_decision_validates_fields() -> None:
    with pytest.raises(ValidationError):
        record_reviewer_decision(
            reviewer_user_id=None,
            decision=ReviewDecision.APPROVED,
            version=1,
            reviewed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source="curated",
        )
    with pytest.raises(ValidationError):
        record_reviewer_decision(
            reviewer_user_id=uuid4(),
            decision="approved",
            version=1,
            reviewed_at=datetime(2024, 1, 1),  # naive
            source="curated",
        )


def test_retired_versions_are_excluded_from_new_practice() -> None:
    concept = uuid4()
    published = ContentItem(uuid4(), concept, ContentKind.LESSON, 1, ContentState.PUBLISHED, "t", "b", "c1")
    retired = ContentItem(uuid4(), concept, ContentKind.LESSON, 2, ContentState.RETIRED, "t", "b", "c2")
    draft = QuestionVersion(uuid4(), concept, "q", 1, ContentState.DRAFT, "p", {}, "e", {}, "c3")
    servable = select_servable_versions([published, retired, draft])
    assert servable == (published,)


# ---------------------------------------------------------------------------
# The exact 15-concept mathematics graph (12.3-12.18)
# ---------------------------------------------------------------------------

EXPECTED_CONCEPT_KEYS = (
    "whole_numbers_and_place_value",
    "division_as_sharing",
    "fraction_as_part_of_a_whole",
    "numerator_and_denominator",
    "equivalent_fractions",
    "comparing_fractions",
    "simplifying_fractions",
    "addition_and_subtraction_with_like_denominators",
    "addition_and_subtraction_with_unlike_denominators",
    "multiplication_of_fractions",
    "division_of_fractions",
    "decimal_place_value",
    "fractions_as_decimals",
    "decimals_as_percentages",
    "percentage_of_a_quantity",
)

EXPECTED_EDGES = {
    ("division_as_sharing", "whole_numbers_and_place_value"),
    ("fraction_as_part_of_a_whole", "division_as_sharing"),
    ("numerator_and_denominator", "fraction_as_part_of_a_whole"),
    ("equivalent_fractions", "numerator_and_denominator"),
    ("comparing_fractions", "equivalent_fractions"),
    ("simplifying_fractions", "equivalent_fractions"),
    ("addition_and_subtraction_with_like_denominators", "numerator_and_denominator"),
    ("addition_and_subtraction_with_unlike_denominators", "equivalent_fractions"),
    ("addition_and_subtraction_with_unlike_denominators", "addition_and_subtraction_with_like_denominators"),
    ("multiplication_of_fractions", "numerator_and_denominator"),
    ("division_of_fractions", "multiplication_of_fractions"),
    ("decimal_place_value", "whole_numbers_and_place_value"),
    ("fractions_as_decimals", "equivalent_fractions"),
    ("fractions_as_decimals", "decimal_place_value"),
    ("decimals_as_percentages", "fractions_as_decimals"),
    ("percentage_of_a_quantity", "decimals_as_percentages"),
}


def test_pack_defines_exactly_the_required_concepts_and_edges() -> None:
    assert tuple(concept["key"] for concept in _CONCEPTS) == EXPECTED_CONCEPT_KEYS
    assert {(concept, prereq) for concept, prereq in _EDGES} == EXPECTED_EDGES
    assert len(_EDGES) == len(EXPECTED_EDGES)


def test_root_concept_has_no_prerequisites() -> None:
    dependents = {concept for concept, _ in _EDGES}
    # The root (12.4) is the only concept that is never a dependent.
    roots = set(EXPECTED_CONCEPT_KEYS) - dependents
    assert roots == {"whole_numbers_and_place_value"}


def test_pack_graph_is_acyclic_and_orders_root_first() -> None:
    payload = mathematics_manifest().payload
    concept_ids = [UUID(str(concept["id"])) for concept in payload["concepts"]]
    edges = [
        ConceptEdge(UUID(str(edge["concept_id"])), UUID(str(edge["prerequisite_concept_id"])))
        for edge in payload["edges"]
    ]
    order = topological_order(concept_ids, edges)
    assert order[0] == UUID(_concept_id("whole_numbers_and_place_value"))


def test_each_concept_has_one_lesson_and_three_questions() -> None:
    payload = mathematics_manifest().payload
    for concept in payload["concepts"]:
        concept_id = concept["id"]
        lessons = [c for c in payload["content_items"] if c["concept_id"] == concept_id and c["kind"] == "lesson"]
        questions = [q for q in payload["question_versions"] if q["concept_id"] == concept_id]
        assert len(lessons) == 1
        assert len(questions) == 3
        for question in questions:
            assert question["prompt"]
            assert question["answer_spec"]
            assert question["explanation"]


def test_manifest_checksum_is_deterministic() -> None:
    assert mathematics_manifest().checksum == mathematics_manifest().checksum


# ---------------------------------------------------------------------------
# Seeding the pack into a database is idempotent (12.1, 12.19, 12.25)
# ---------------------------------------------------------------------------


def _engine():
    return create_engine("sqlite+pysqlite:///:memory:")


def _create_curriculum_tables(engine) -> None:
    from app.persistence.models import metadata

    metadata.create_all(
        engine,
        tables=[
            users,
            subjects,
            concepts,
            concept_edges,
            content_items,
            question_versions,
            content_reviews,
            curriculum_seed_manifests,
        ],
    )


def test_apply_pack_seeds_all_content_and_is_idempotent() -> None:
    engine = _engine()
    _create_curriculum_tables(engine)
    manifest = mathematics_manifest()
    with engine.begin() as connection:
        assert apply_curriculum_manifest(connection, manifest) is True
        assert apply_curriculum_manifest(connection, manifest) is False

        assert connection.scalar(select(func.count()).select_from(concepts)) == 15
        assert connection.scalar(select(func.count()).select_from(concept_edges)) == 16
        assert connection.scalar(select(func.count()).select_from(content_items)) == 15
        assert connection.scalar(select(func.count()).select_from(question_versions)) == 45
        assert connection.scalar(select(func.count()).select_from(content_reviews)) == 60
        # The curriculum reviewer account is provisioned by the pack.
        reviewer = connection.scalar(select(users.c.id).where(users.c.id == REVIEWER_ID))
        assert reviewer == REVIEWER_ID
        # Every seeded review is an approving decision recorded before publication.
        decisions = connection.execute(select(content_reviews.c.decision)).scalars().all()
        assert set(decisions) == {"approved"}


def test_apply_pack_publishes_only_after_acyclic_validation() -> None:
    """A cyclic pack must be rejected before any concept is written (12.2, 12.23)."""
    engine = _engine()
    _create_curriculum_tables(engine)
    manifest = mathematics_manifest()
    payload = dict(manifest.payload)
    # Inject a back edge to create a cycle in an otherwise valid graph.
    payload = {**payload, "edges": list(payload["edges"]) + [
        {
            "id": str(uuid4()),
            "concept_id": _concept_id("whole_numbers_and_place_value"),
            "prerequisite_concept_id": _concept_id("percentage_of_a_quantity"),
        }
    ]}
    from app.persistence.seeds import CurriculumManifest, manifest_checksum

    cyclic = CurriculumManifest(pack="cyclic-math", version="1", payload=payload, checksum=manifest_checksum(payload))
    with engine.begin() as connection:
        with pytest.raises(CyclicPrerequisiteError):
            apply_curriculum_manifest(connection, cyclic)
        assert connection.scalar(select(func.count()).select_from(concepts)) == 0
