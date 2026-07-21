from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, and_, select

from app.domain.curriculum import ConceptEdge, validate_acyclic
from app.persistence.models import (
    bkt_parameter_sets,
    concept_edges,
    concepts,
    content_items,
    content_reviews,
    curriculum_seed_manifests,
    profiles,
    question_versions,
    subjects,
    user_relationships,
    users,
)

LOCAL_LEARNER_ID = UUID("00000000-0000-4000-8000-000000000001")
LOCAL_PARENT_ID = UUID("00000000-0000-4000-8000-000000000002")
LOCAL_TEACHER_ID = UUID("00000000-0000-4000-8000-000000000003")
LOCAL_PARENT_RELATIONSHIP_ID = UUID("00000000-0000-4000-8000-000000000011")
LOCAL_TEACHER_RELATIONSHIP_ID = UUID("00000000-0000-4000-8000-000000000012")

#: The stable default BKT parameter set applied when a concept has no override.
DEFAULT_BKT_PARAMETER_SET_ID = UUID("00000000-0000-4000-8000-000000000021")
DEFAULT_BKT_PARAMETER_KEY = "default"
DEFAULT_BKT_PARAMETER_VERSION = 1


class SeedConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CurriculumManifest:
    pack: str
    version: str
    payload: dict[str, Any]
    checksum: str

    @classmethod
    def load(cls, path: Path) -> "CurriculumManifest":
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document["payload"]
        actual = manifest_checksum(payload)
        expected = document.get("checksum", actual)
        if actual != expected:
            raise SeedConflict("Curriculum manifest checksum does not match its payload")
        return cls(pack=document["pack"], version=document["version"], payload=payload, checksum=actual)


def manifest_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _upsert(connection: Connection, table: Any, key: dict[str, Any], values: dict[str, Any]) -> None:
    predicate = and_(*(table.c[name] == value for name, value in key.items()))
    exists = connection.execute(select(table.c[next(iter(key))]).where(predicate)).first()
    if exists is None:
        connection.execute(table.insert().values(**key, **values))
    else:
        connection.execute(table.update().where(predicate).values(**values))


def seed_local_personas(connection: Connection) -> None:
    """Create stable loopback-only development personas; safe to run repeatedly."""
    personas = (
        (LOCAL_LEARNER_ID, "learner@local.fastlearner", "Local Learner"),
        (LOCAL_PARENT_ID, "parent@local.fastlearner", "Local Parent"),
        (LOCAL_TEACHER_ID, "teacher@local.fastlearner", "Local Teacher"),
    )
    for user_id, email, display_name in personas:
        _upsert(
            connection,
            users,
            {"id": user_id},
            {"email": email, "display_name": display_name, "status": "active"},
        )
    _upsert(
        connection,
        profiles,
        {"user_id": LOCAL_LEARNER_ID},
        {
            "owner_user_id": LOCAL_LEARNER_ID,
            "grade_level": 5,
            "timezone": "UTC",
            "study_preferences": {"session_minutes": 25, "daily_limit_minutes": 90},
        },
    )
    relationships = (
        (
            LOCAL_PARENT_RELATIONSHIP_ID,
            LOCAL_PARENT_ID,
            "parent",
            ["dashboard:read", "assignments:read", "learning:read", "memory:read", "pathways:read"],
        ),
        (
            LOCAL_TEACHER_RELATIONSHIP_ID,
            LOCAL_TEACHER_ID,
            "teacher",
            ["dashboard:read", "assignments:read", "learning:read"],
        ),
    )
    for relationship_id, observer_id, role, scope in relationships:
        _upsert(
            connection,
            user_relationships,
            {"id": relationship_id},
            {
                "owner_user_id": LOCAL_LEARNER_ID,
                "learner_user_id": LOCAL_LEARNER_ID,
                "observer_user_id": observer_id,
                "role": role,
                "permission_scope": scope,
                "status": "active",
                "expires_at": None,
            },
        )


def seed_default_bkt_parameters(connection: Connection) -> None:
    """Seed the default versioned BKT parameter set; safe to run repeatedly.

    The Learning_Service applies this active parameter set when a concept has no
    mastery record yet (Requirement 14.6). Values satisfy ``0 <= p_* <= 1`` and
    keep the posterior denominators nonzero for any prior in ``[0, 1]``.
    """
    _upsert(
        connection,
        bkt_parameter_sets,
        {"id": DEFAULT_BKT_PARAMETER_SET_ID},
        {
            "key": DEFAULT_BKT_PARAMETER_KEY,
            "version": DEFAULT_BKT_PARAMETER_VERSION,
            "prior": "0.3000000",
            "transition": "0.1000000",
            "slip": "0.1000000",
            "guess": "0.2000000",
            "status": "active",
        },
    )


def _uuid_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: UUID(value) if (key == "id" or key.endswith("_id")) and isinstance(value, str) else value
        for key, value in values.items()
    }


def _validate_manifest_dag(payload: dict[str, Any]) -> None:
    """Reject publication of a cyclic prerequisite graph (Requirements 12.2, 12.23).

    Raises :class:`app.domain.curriculum.CyclicPrerequisiteError` identifying the
    involved concept edges when a cycle is present.
    """
    concept_ids = [UUID(str(concept["id"])) for concept in payload.get("concepts", [])]
    edges = [
        ConceptEdge(
            concept_id=UUID(str(edge["concept_id"])),
            prerequisite_concept_id=UUID(str(edge["prerequisite_concept_id"])),
        )
        for edge in payload.get("edges", [])
    ]
    validate_acyclic(concept_ids, edges)


def _insert_if_absent(connection: Connection, table: Any, values: dict[str, Any]) -> None:
    """Insert an immutable versioned row once, leaving any existing row intact.

    Content and question versions are immutable (Requirement 12.1), so an
    existing row with the same identifier is never rewritten.
    """
    identifier = values["id"]
    exists = connection.execute(select(table.c.id).where(table.c.id == identifier)).first()
    if exists is None:
        connection.execute(table.insert().values(**values))


def apply_curriculum_manifest(connection: Connection, manifest: CurriculumManifest) -> bool:
    """Apply a versioned curriculum DAG manifest once, rejecting changed same-version data.

    The prerequisite graph is validated as acyclic before any concept is
    published (Requirement 12.2). Concepts, edges, immutable content items,
    immutable question versions, reviewer accounts, and approving reviews are all
    seeded within the caller's transaction, and applying the same pack/version a
    second time is a no-op (idempotent manifest checksum).
    """
    existing = connection.execute(
        select(curriculum_seed_manifests.c.checksum).where(
            curriculum_seed_manifests.c.pack == manifest.pack,
            curriculum_seed_manifests.c.version == manifest.version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing != manifest.checksum:
            raise SeedConflict(
                f"Curriculum pack {manifest.pack}@{manifest.version} already has another checksum"
            )
        return False

    # Acyclic validation gates publication before anything is written.
    _validate_manifest_dag(manifest.payload)

    subject_values = _uuid_values(dict(manifest.payload["subject"]))
    subject_id = subject_values.pop("id")
    _upsert(connection, subjects, {"id": subject_id}, subject_values)

    # Reviewer accounts must exist before their approving reviews are recorded.
    for raw_reviewer in manifest.payload.get("reviewers", []):
        reviewer_values = _uuid_values(dict(raw_reviewer))
        reviewer_id = reviewer_values.pop("id")
        _upsert(connection, users, {"id": reviewer_id}, reviewer_values)

    for raw_concept in manifest.payload.get("concepts", []):
        concept_values = _uuid_values(dict(raw_concept))
        concept_id = concept_values.pop("id")
        concept_values.setdefault("subject_id", subject_id)
        _upsert(connection, concepts, {"id": concept_id}, concept_values)

    for raw_edge in manifest.payload.get("edges", []):
        edge_values = _uuid_values(dict(raw_edge))
        edge_id = edge_values.pop("id")
        edge_values.setdefault("subject_id", subject_id)
        _upsert(connection, concept_edges, {"id": edge_id}, edge_values)

    for raw_item in manifest.payload.get("content_items", []):
        item_values = _uuid_values(dict(raw_item))
        item_values.setdefault("subject_id", subject_id)
        _insert_if_absent(connection, content_items, item_values)

    for raw_question in manifest.payload.get("question_versions", []):
        question_values = _uuid_values(dict(raw_question))
        question_values.setdefault("subject_id", subject_id)
        _insert_if_absent(connection, question_versions, question_values)

    for raw_review in manifest.payload.get("reviews", []):
        review_values = _uuid_values(dict(raw_review))
        review_values["reviewed_at"] = _parse_instant(review_values["reviewed_at"])
        _insert_if_absent(connection, content_reviews, review_values)

    from uuid import NAMESPACE_URL, uuid5

    connection.execute(
        curriculum_seed_manifests.insert().values(
            id=uuid5(NAMESPACE_URL, f"fastlearner:curriculum:{manifest.pack}:{manifest.version}"),
            pack=manifest.pack,
            version=manifest.version,
            checksum=manifest.checksum,
        )
    )
    return True


def _parse_instant(value: Any) -> datetime:
    """Parse an ISO-8601 review timestamp into a timezone-aware instant."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
