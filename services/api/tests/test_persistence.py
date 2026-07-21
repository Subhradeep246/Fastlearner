from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select, text

from app.persistence.checks import (
    DatabaseMigrationRequired,
    assert_revision_compatible,
    assert_schema_matches_models,
    check_revision,
    revision_chain,
)
from app.persistence.models import (
    ALL_TABLE_NAMES,
    OWNED_TABLE_NAMES,
    concept_edges,
    concepts,
    curriculum_seed_manifests,
    metadata,
    profiles,
    subjects,
    user_relationships,
    users,
    utc_datetime,
)
from app.persistence.seeds import (
    CurriculumManifest,
    SeedConflict,
    apply_curriculum_manifest,
    manifest_checksum,
    seed_local_personas,
)

EXPECTED_CHAIN = (
    "0001_identity",
    "0002_curriculum",
    "0003_work",
    "0004_learning",
    "0005_memory_vector",
    "0006_actions",
    "0007_outbox",
    "0008_lifecycle",
    "0009_operations",
    "0010_constraints",
    "0011_owner_indexes",
)


def _engine():
    return create_engine("sqlite+pysqlite:///:memory:")


def test_revision_history_is_one_linear_bounded_chain() -> None:
    assert revision_chain() == EXPECTED_CHAIN


def test_canonical_schema_has_all_domains_and_owner_first_indexes() -> None:
    expected_tables = {
        "users", "profiles", "user_relationships", "devices", "sessions",
        "subjects", "concepts", "concept_edges", "content_items", "question_versions",
        "assignments", "assignment_tasks", "study_blocks", "learning_events", "mastery_state",
        "sources", "memory_episodes", "source_chunks", "consents", "action_proposals",
        "idempotency_records", "audit_records", "outbox_jobs", "graph_sync_state",
        "deletion_requests", "export_requests", "job_runs", "rule_versions", "diagnostic_records",
    }
    assert expected_tables <= ALL_TABLE_NAMES
    for table_name in OWNED_TABLE_NAMES:
        owner_indexes = [
            index for index in metadata.tables[table_name].indexes
            if index.info.get("migration_group") == "owner"
        ]
        assert len(owner_indexes) == 1
        assert list(owner_indexes[0].columns)[0].name == "owner_user_id"


def test_timestamp_boundary_rejects_naive_values_and_normalizes_utc() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        utc_datetime(datetime(2025, 1, 1))
    value = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert utc_datetime(value) == value


def test_local_persona_seed_is_idempotent() -> None:
    engine = _engine()
    metadata.create_all(engine, tables=[users, profiles, user_relationships])
    with engine.begin() as connection:
        seed_local_personas(connection)
        seed_local_personas(connection)
        assert connection.scalar(select(func.count()).select_from(users)) == 3
        assert connection.scalar(select(func.count()).select_from(profiles)) == 1
        assert connection.scalar(select(func.count()).select_from(user_relationships)) == 2


def _curriculum_manifest(version: str = "1", title: str = "Test Mathematics") -> CurriculumManifest:
    subject_id = "10000000-0000-4000-8000-000000000001"
    root_id = "10000000-0000-4000-8000-000000000002"
    child_id = "10000000-0000-4000-8000-000000000003"
    payload = {
        "subject": {
            "id": subject_id,
            "owner_user_id": None,
            "slug": "test-math",
            "title": title,
            "kind": "curriculum",
            "status": "active",
            "archived_at": None,
        },
        "concepts": [
            {"id": root_id, "key": "root", "title": "Root", "grade_min": 3, "grade_max": 5, "status": "published", "version": 1},
            {"id": child_id, "key": "child", "title": "Child", "grade_min": 3, "grade_max": 5, "status": "published", "version": 1},
        ],
        "edges": [
            {"id": "10000000-0000-4000-8000-000000000004", "concept_id": child_id, "prerequisite_concept_id": root_id}
        ],
    }
    return CurriculumManifest(
        pack="test-math",
        version=version,
        payload=payload,
        checksum=manifest_checksum(payload),
    )


def test_versioned_curriculum_seed_is_idempotent_and_tracks_checksum() -> None:
    engine = _engine()
    metadata.create_all(
        engine,
        tables=[users, subjects, concepts, concept_edges, curriculum_seed_manifests],
    )
    with engine.begin() as connection:
        manifest = _curriculum_manifest()
        assert apply_curriculum_manifest(connection, manifest) is True
        assert apply_curriculum_manifest(connection, manifest) is False
        assert connection.scalar(select(func.count()).select_from(subjects)) == 1
        assert connection.scalar(select(func.count()).select_from(concepts)) == 2
        assert connection.scalar(select(func.count()).select_from(concept_edges)) == 1
        assert connection.scalar(select(curriculum_seed_manifests.c.checksum)) == manifest.checksum


def test_curriculum_seed_rejects_changed_payload_for_same_version() -> None:
    engine = _engine()
    metadata.create_all(
        engine,
        tables=[users, subjects, concepts, concept_edges, curriculum_seed_manifests],
    )
    with engine.begin() as connection:
        apply_curriculum_manifest(connection, _curriculum_manifest())
        with pytest.raises(SeedConflict, match="another checksum"):
            apply_curriculum_manifest(connection, _curriculum_manifest(title="Changed"))


def test_revision_compatibility_returns_typed_safe_error() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        assert check_revision(connection).current is None
        with pytest.raises(DatabaseMigrationRequired) as caught:
            assert_revision_compatible(connection)
        assert caught.value.code == "database_migration_required"
        assert "database_migration_required" in str(caught.value.safe_payload())

        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0011_owner_indexes')")
        )
        assert_revision_compatible(connection)


def test_model_schema_consistency_check_accepts_canonical_schema() -> None:
    engine = _engine()
    metadata.create_all(engine)
    with engine.connect() as connection:
        assert_schema_matches_models(connection)
