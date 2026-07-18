from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.schema import Column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


class LifecycleStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class JobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


def timestamps() -> tuple[Column[Any], Column[Any]]:
    return (
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )


def owned_id_columns() -> tuple[Column[Any], Column[Any]]:
    return (
        Column("id", Uuid, primary_key=True),
        Column("owner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    )


def status_check(name: str, values: tuple[str, ...], column: str = "status") -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)

# Identity and authorization.
users = Table(
    "users", metadata,
    Column("id", Uuid, primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("display_name", String(120), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    *timestamps(),
    status_check("user_status", ("active", "disabled", "deleted")),
)
profiles = Table(
    "profiles", metadata,
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("owner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("grade_level", Integer, nullable=False),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("study_preferences", JSON, nullable=False, server_default=text("'{}'")),
    *timestamps(),
    CheckConstraint("grade_level BETWEEN 3 AND 12", name="profile_grade_level"),
)
user_relationships = Table(
    "user_relationships", metadata,
    *owned_id_columns(),
    Column("learner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("observer_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(16), nullable=False),
    Column("permission_scope", JSON, nullable=False, server_default=text("'[]'")),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("expires_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("relationship_role", ("parent", "teacher"), "role"),
    status_check("relationship_status", ("active", "expired", "revoked")),
    CheckConstraint("owner_user_id = learner_user_id", name="relationship_owner_is_learner"),
)
devices = Table(
    "devices", metadata,
    *owned_id_columns(),
    Column("name", String(120), nullable=False),
    Column("platform", String(24), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("last_seen_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("device_status", ("active", "revoked", "unavailable")),
)
sessions = Table(
    "sessions", metadata,
    *owned_id_columns(),
    Column("actor_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("session_version", Integer, nullable=False, server_default="1"),
    *timestamps(),
    status_check("session_status", ("active", "expired", "revoked")),
)

# Curriculum and reviewed content.
subjects = Table(
    "subjects", metadata,
    Column("id", Uuid, primary_key=True),
    Column("owner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE")),
    Column("slug", String(96), nullable=False),
    Column("title", String(160), nullable=False),
    Column("kind", String(24), nullable=False, server_default="curriculum"),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("archived_at", DateTime(timezone=True)),
    *timestamps(),
    UniqueConstraint("owner_user_id", "slug", name="uq_subjects_owner_slug"),
    status_check("subject_kind", ("curriculum", "school_managed", "learner_created"), "kind"),
    status_check("subject_status", ("active", "archived")),
)
concepts = Table(
    "concepts", metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
    Column("key", String(120), nullable=False),
    Column("title", String(180), nullable=False),
    Column("grade_min", Integer, nullable=False),
    Column("grade_max", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="draft"),
    Column("version", Integer, nullable=False, server_default="1"),
    *timestamps(),
    UniqueConstraint("subject_id", "key", name="uq_concepts_subject_key"),
    CheckConstraint("grade_min BETWEEN 3 AND 12 AND grade_max BETWEEN grade_min AND 12", name="concept_grade_range"),
    status_check("concept_status", ("draft", "published", "retired")),
)
concept_edges = Table(
    "concept_edges", metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False),
    Column("prerequisite_concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False),
    *timestamps(),
    UniqueConstraint("concept_id", "prerequisite_concept_id", name="uq_concept_edges_pair"),
    CheckConstraint("concept_id <> prerequisite_concept_id", name="concept_edge_no_self"),
)

content_items = Table(
    "content_items", metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(24), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="draft"),
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("checksum", String(64), nullable=False),
    *timestamps(),
    UniqueConstraint("concept_id", "kind", "version", name="uq_content_items_version"),
    UniqueConstraint("checksum", name="uq_content_items_checksum"),
    status_check("content_kind", ("lesson", "hint", "explanation"), "kind"),
    status_check("content_status", ("draft", "published", "retired")),
)
question_versions = Table(
    "question_versions", metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False),
    Column("question_key", String(120), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="draft"),
    Column("prompt", Text, nullable=False),
    Column("answer_spec", JSON, nullable=False),
    Column("explanation", Text, nullable=False),
    Column("provenance", JSON, nullable=False, server_default=text("'{}'")),
    Column("checksum", String(64), nullable=False),
    *timestamps(),
    UniqueConstraint("question_key", "version", name="uq_question_versions_key_version"),
    UniqueConstraint("checksum", name="uq_question_versions_checksum"),
    status_check("question_status", ("draft", "published", "retired")),
)
content_reviews = Table(
    "content_reviews", metadata,
    Column("id", Uuid, primary_key=True),
    Column("content_item_id", Uuid, ForeignKey("content_items.id", ondelete="CASCADE")),
    Column("question_version_id", Uuid, ForeignKey("question_versions.id", ondelete="CASCADE")),
    Column("reviewer_user_id", Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("decision", String(24), nullable=False),
    Column("notes", Text),
    Column("reviewed_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
    CheckConstraint("(content_item_id IS NULL) <> (question_version_id IS NULL)", name="content_review_one_target"),
    status_check("content_review_decision", ("approved", "rejected", "changes_requested"), "decision"),
)
curriculum_seed_manifests = Table(
    "curriculum_seed_manifests", metadata,
    Column("id", Uuid, primary_key=True),
    Column("pack", String(96), nullable=False),
    Column("version", String(32), nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("pack", "version", name="uq_curriculum_seed_pack_version"),
)

# Assignments, goals, and planning.
assignments = Table(
    "assignments", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False),
    Column("title", String(240), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("estimated_minutes", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("brief_source_id", Uuid, ForeignKey("sources.id", name="fk_assignments_brief_source_id_sources", use_alter=True, ondelete="SET NULL")),
    Column("deleted_at", DateTime(timezone=True)),
    *timestamps(),
    CheckConstraint("estimated_minutes > 0", name="assignment_positive_effort"),
    status_check("assignment_status", ("pending", "in_progress", "done", "archived")),
)
assignment_tasks = Table(
    "assignment_tasks", metadata,
    *owned_id_columns(),
    Column("assignment_id", Uuid, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(240), nullable=False),
    Column("position", Integer, nullable=False),
    Column("estimated_minutes", Integer, nullable=False),
    Column("due_at", DateTime(timezone=True)),
    Column("status", String(24), nullable=False, server_default="pending"),
    *timestamps(),
    UniqueConstraint("assignment_id", "position", name="uq_assignment_tasks_position"),
    CheckConstraint("position >= 0 AND estimated_minutes > 0", name="assignment_task_values"),
    status_check("assignment_task_status", ("pending", "in_progress", "done", "archived")),
)
availability_windows = Table(
    "availability_windows", metadata,
    *owned_id_columns(),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("recurrence_rule", String(256)),
    *timestamps(),
    CheckConstraint("ends_at > starts_at", name="availability_positive_duration"),
)
study_blocks = Table(
    "study_blocks", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False),
    Column("assignment_id", Uuid, ForeignKey("assignments.id", ondelete="SET NULL")),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="SET NULL")),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("status", String(24), nullable=False, server_default="planned"),
    Column("reason_json", JSON, nullable=False),
    Column("reason_text", Text, nullable=False),
    *timestamps(),
    CheckConstraint("ends_at > starts_at", name="study_block_positive_duration"),
    status_check("study_block_status", ("planned", "active", "skipped", "done")),
)

study_block_reason_history = Table(
    "study_block_reason_history", metadata,
    *owned_id_columns(),
    Column("study_block_id", Uuid, ForeignKey("study_blocks.id", ondelete="CASCADE"), nullable=False),
    Column("reason_json", JSON, nullable=False),
    Column("reason_text", Text, nullable=False),
    *timestamps(),
)
effort_entries = Table(
    "effort_entries", metadata,
    *owned_id_columns(),
    Column("assignment_id", Uuid, ForeignKey("assignments.id", ondelete="SET NULL")),
    Column("study_block_id", Uuid, ForeignKey("study_blocks.id", ondelete="SET NULL")),
    Column("minutes", Integer, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
    CheckConstraint("minutes > 0", name="effort_positive_minutes"),
    CheckConstraint("assignment_id IS NOT NULL OR study_block_id IS NOT NULL", name="effort_has_target"),
)
goals = Table(
    "goals", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="SET NULL")),
    Column("title", String(240), nullable=False),
    Column("target_at", DateTime(timezone=True)),
    Column("status", String(24), nullable=False, server_default="active"),
    *timestamps(),
    status_check("goal_status", ("active", "completed", "archived")),
)

# Learning state and deterministic rule evidence.
bkt_parameter_sets = Table(
    "bkt_parameter_sets", metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", String(96), nullable=False),
    Column("version", Integer, nullable=False),
    Column("prior", Numeric(8, 7), nullable=False),
    Column("transition", Numeric(8, 7), nullable=False),
    Column("slip", Numeric(8, 7), nullable=False),
    Column("guess", Numeric(8, 7), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    *timestamps(),
    UniqueConstraint("key", "version", name="uq_bkt_parameter_sets_key_version"),
    CheckConstraint("prior BETWEEN 0 AND 1 AND transition BETWEEN 0 AND 1 AND slip BETWEEN 0 AND 1 AND guess BETWEEN 0 AND 1", name="bkt_probability_bounds"),
    status_check("bkt_parameter_status", ("active", "retired")),
)
learning_events = Table(
    "learning_events", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False),
    Column("question_version_id", Uuid, ForeignKey("question_versions.id", ondelete="RESTRICT"), nullable=False),
    Column("operation_scope", String(96), nullable=False),
    Column("idempotency_key", String(160), nullable=False),
    Column("correct", Boolean, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("hint_used", Boolean, nullable=False, server_default=text("false")),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
    UniqueConstraint("owner_user_id", "operation_scope", "idempotency_key", name="uq_learning_events_idempotency"),
    CheckConstraint("duration_ms >= 0 AND retry_count >= 0", name="learning_event_nonnegative_evidence"),
)
mastery_state = Table(
    "mastery_state", metadata,
    Column("owner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True),
    Column("probability", Numeric(8, 7), nullable=False),
    Column("parameter_set_id", Uuid, ForeignKey("bkt_parameter_sets.id", ondelete="RESTRICT"), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    *timestamps(),
    CheckConstraint("probability BETWEEN 0 AND 1", name="mastery_probability_bounds"),
)
review_state = Table(
    "review_state", metadata,
    Column("owner_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True),
    Column("rule_version", String(64), nullable=False),
    Column("interval_days", Integer, nullable=False),
    Column("ease_factor", Numeric(6, 3), nullable=False),
    Column("repetitions", Integer, nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
    CheckConstraint("interval_days >= 0 AND repetitions >= 0 AND ease_factor > 0", name="review_state_values"),
)
mastery_snapshots = Table(
    "mastery_snapshots", metadata,
    *owned_id_columns(),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False),
    Column("learning_event_id", Uuid, ForeignKey("learning_events.id", ondelete="CASCADE"), nullable=False),
    Column("probability", Numeric(8, 7), nullable=False),
    Column("rule_version", String(64), nullable=False),
    *timestamps(),
    CheckConstraint("probability BETWEEN 0 AND 1", name="mastery_snapshot_probability_bounds"),
)
recommendations = Table(
    "recommendations", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", Uuid, ForeignKey("concepts.id", ondelete="CASCADE")),
    Column("kind", String(48), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("rule_version", String(64), nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("reason", Text, nullable=False),
    *timestamps(),
    status_check("recommendation_status", ("active", "accepted", "dismissed", "expired")),
)

# Deliberate memory, provenance, vector retrieval, and consent.
sources = Table(
    "sources", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="SET NULL")),
    Column("kind", String(48), nullable=False),
    Column("title", String(240)),
    Column("uri", Text),
    Column("content_checksum", String(64), nullable=False),
    Column("provenance", JSON, nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("deleted_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("source_status", ("active", "quarantined", "deleted")),
)
memory_episodes = Table(
    "memory_episodes", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="SET NULL")),
    Column("source_id", Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", String(48), nullable=False),
    Column("content", Text, nullable=False),
    Column("visibility", String(24), nullable=False, server_default="private"),
    Column("user_confidence", Float),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("deleted_at", DateTime(timezone=True)),
    *timestamps(),
    CheckConstraint("length(content) > 0", name="memory_episode_content_not_empty"),
    CheckConstraint("user_confidence IS NULL OR (user_confidence >= 0 AND user_confidence <= 1)", name="memory_episode_confidence"),
    status_check("memory_episode_status", ("active", "deleted")),
)
source_chunks = Table(
    "source_chunks", metadata,
    *owned_id_columns(),
    Column("subject_id", Uuid, ForeignKey("subjects.id", ondelete="SET NULL")),
    Column("source_id", Uuid, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
    Column("episode_id", Uuid, ForeignKey("memory_episodes.id", ondelete="CASCADE")),
    Column("position", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding", Vector(1536)),
    Column("metadata_json", JSON, nullable=False, server_default=text("'{}'")),
    Column("deleted_at", DateTime(timezone=True)),
    *timestamps(),
    UniqueConstraint("source_id", "position", name="uq_source_chunks_position"),
    CheckConstraint("position >= 0", name="source_chunk_position"),
)
memory_retrieval_log = Table(
    "memory_retrieval_log", metadata,
    *owned_id_columns(),
    Column("query_hash", String(64), nullable=False),
    Column("result_ids", JSON, nullable=False),
    Column("filter_hash", String(64), nullable=False),
    Column("result_count", Integer, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
    CheckConstraint("result_count >= 0", name="memory_retrieval_count"),
)
consents = Table(
    "consents", metadata,
    *owned_id_columns(),
    Column("kind", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("granted_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    *timestamps(),
    UniqueConstraint("owner_user_id", "kind", "policy_version", name="uq_consents_owner_kind_version"),
    status_check("consent_status", ("granted", "revoked", "declined")),
)
auto_save_rules = Table(
    "auto_save_rules", metadata,
    *owned_id_columns(),
    Column("name", String(120), nullable=False),
    Column("source_kind", String(48), nullable=False),
    Column("consent_id", Uuid, ForeignKey("consents.id", ondelete="CASCADE"), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("rule_json", JSON, nullable=False),
    *timestamps(),
    UniqueConstraint("owner_user_id", "name", name="uq_auto_save_rules_owner_name"),
)

# Proposed actions, idempotency, and audit.
action_proposals = Table(
    "action_proposals", metadata,
    *owned_id_columns(),
    Column("actor_user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("action_type", String(96), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("payload", JSON, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("action_proposal_status", ("pending", "confirmed", "rejected", "expired")),
)
idempotency_records = Table(
    "idempotency_records", metadata,
    *owned_id_columns(),
    Column("operation", String(96), nullable=False),
    Column("key", String(160), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("response_status", Integer),
    Column("response_body_hash", String(64)),
    Column("result_ref", Uuid),
    Column("expires_at", DateTime(timezone=True)),
    *timestamps(),
    UniqueConstraint("owner_user_id", "operation", "key", name="uq_idempotency_records_operation_key"),
)
audit_records = Table(
    "audit_records", metadata,
    *owned_id_columns(),
    Column("actor_user_id", Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("action", String(96), nullable=False),
    Column("resource_kind", String(96), nullable=False),
    Column("resource_id", Uuid),
    Column("outcome", String(32), nullable=False),
    Column("details", JSON, nullable=False, server_default=text("'{}'")),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    *timestamps(),
)

# Transactional outbox, lifecycle, and operational state.
outbox_jobs = Table(
    "outbox_jobs", metadata,
    *owned_id_columns(),
    Column("kind", String(96), nullable=False),
    Column("deduplication_key", String(192), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("leased_until", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("last_error_code", String(96)),
    *timestamps(),
    UniqueConstraint("kind", "deduplication_key", name="uq_outbox_jobs_deduplication"),
    status_check("outbox_job_status", tuple(value.value for value in JobStatus)),
    CheckConstraint("attempt_count >= 0", name="outbox_attempt_count"),
)
graph_sync_state = Table(
    "graph_sync_state", metadata,
    *owned_id_columns(),
    Column("episode_id", Uuid, ForeignKey("memory_episodes.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("graph_group", String(192), nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("last_error_code", String(96)),
    Column("synced_at", DateTime(timezone=True)),
    *timestamps(),
    UniqueConstraint("episode_id", name="uq_graph_sync_state_episode"),
    status_check("graph_sync_status", ("pending", "synced", "failed", "retracted")),
)
deletion_requests = Table(
    "deletion_requests", metadata,
    *owned_id_columns(),
    Column("kind", String(48), nullable=False),
    Column("target_id", Uuid),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("deletion_request_status", ("pending", "processing", "completed", "failed")),
)
export_requests = Table(
    "export_requests", metadata,
    *owned_id_columns(),
    Column("format_version", String(32), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("artifact_uri", Text),
    Column("checksum", String(64)),
    Column("expires_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    *timestamps(),
    status_check("export_request_status", ("pending", "processing", "completed", "failed", "expired")),
)
job_runs = Table(
    "job_runs", metadata,
    *owned_id_columns(),
    Column("outbox_job_id", Uuid, ForeignKey("outbox_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("status", String(24), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("error_code", String(96)),
    *timestamps(),
    CheckConstraint("attempt > 0", name="job_run_attempt"),
    status_check("job_run_status", ("leased", "succeeded", "retry_wait", "dead_letter", "cancelled")),
)
rule_versions = Table(
    "rule_versions", metadata,
    Column("id", Uuid, primary_key=True),
    Column("domain", String(64), nullable=False),
    Column("key", String(96), nullable=False),
    Column("version", String(64), nullable=False),
    Column("configuration", JSON, nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    *timestamps(),
    UniqueConstraint("domain", "key", "version", name="uq_rule_versions_domain_key_version"),
    status_check("rule_version_status", ("active", "retired")),
)
diagnostic_records = Table(
    "diagnostic_records", metadata,
    *owned_id_columns(),
    Column("kind", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("aggregate_data", JSON, nullable=False),
    Column("consent_id", Uuid, ForeignKey("consents.id", ondelete="CASCADE"), nullable=False),
    *timestamps(),
)

TABLE_GROUPS: dict[str, tuple[str, ...]] = {
    "identity": ("users", "profiles", "user_relationships", "devices", "sessions"),
    "curriculum": ("subjects", "concepts", "concept_edges", "content_items", "question_versions", "content_reviews", "curriculum_seed_manifests"),
    "work": ("assignments", "assignment_tasks", "availability_windows", "study_blocks", "study_block_reason_history", "effort_entries", "goals"),
    "learning": ("bkt_parameter_sets", "learning_events", "mastery_state", "review_state", "mastery_snapshots", "recommendations"),
    "memory": ("sources", "memory_episodes", "source_chunks", "memory_retrieval_log", "consents", "auto_save_rules"),
    "actions": ("action_proposals", "idempotency_records", "audit_records"),
    "outbox": ("outbox_jobs",),
    "lifecycle": ("graph_sync_state", "deletion_requests", "export_requests"),
    "operations": ("job_runs", "rule_versions", "diagnostic_records"),
}

# Indexes are declared canonically here and installed in bounded migration revisions.
for _table in metadata.tables.values():
    if "owner_user_id" not in _table.c:
        continue
    _columns = [_table.c.owner_user_id]
    if "subject_id" in _table.c:
        _columns.append(_table.c.subject_id)
    if "deleted_at" in _table.c:
        _columns.append(_table.c.deleted_at)
    elif "status" in _table.c:
        _columns.append(_table.c.status)
    elif "created_at" in _table.c:
        _columns.append(_table.c.created_at)
    Index(f"ix_{_table.name}_owner_scope", *_columns, info={"migration_group": "owner"})

Index("ix_concepts_subject_status", concepts.c.subject_id, concepts.c.status)
Index("ix_concept_edges_subject", concept_edges.c.subject_id)
Index("ix_content_items_concept_status", content_items.c.concept_id, content_items.c.status)
Index("ix_question_versions_concept_status", question_versions.c.concept_id, question_versions.c.status)
Index("ix_study_blocks_time_range", study_blocks.c.starts_at, study_blocks.c.ends_at)
Index("ix_source_chunks_source_position", source_chunks.c.source_id, source_chunks.c.position)
Index("ix_outbox_jobs_ready", outbox_jobs.c.status, outbox_jobs.c.available_at)
Index("ix_job_runs_outbox_attempt", job_runs.c.outbox_job_id, job_runs.c.attempt)
Index(
    "ux_user_relationships_active",
    user_relationships.c.learner_user_id,
    user_relationships.c.observer_user_id,
    user_relationships.c.role,
    unique=True,
    postgresql_where=text("status = 'active'"),
    sqlite_where=text("status = 'active'"),
)

ALL_TABLE_NAMES = frozenset(metadata.tables)
OWNED_TABLE_NAMES = frozenset(
    table.name for table in metadata.tables.values() if "owner_user_id" in table.c
)


def utc_datetime(value: datetime) -> datetime:
    """Reject naive timestamps at persistence boundaries."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Canonical timestamps must include a UTC offset")
    return value.astimezone(timezone.utc)


def require_owner(value: UUID | None) -> UUID:
    """Reject missing owner scope before a repository statement is built."""
    if value is None:
        raise ValueError("owner_user_id is required")
    return value
