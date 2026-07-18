from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, and_, select

from app.persistence.models import (
    concept_edges,
    concepts,
    curriculum_seed_manifests,
    profiles,
    subjects,
    user_relationships,
    users,
)

LOCAL_LEARNER_ID = UUID("00000000-0000-4000-8000-000000000001")
LOCAL_PARENT_ID = UUID("00000000-0000-4000-8000-000000000002")
LOCAL_TEACHER_ID = UUID("00000000-0000-4000-8000-000000000003")
LOCAL_PARENT_RELATIONSHIP_ID = UUID("00000000-0000-4000-8000-000000000011")
LOCAL_TEACHER_RELATIONSHIP_ID = UUID("00000000-0000-4000-8000-000000000012")


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


def _uuid_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: UUID(value) if (key == "id" or key.endswith("_id")) and isinstance(value, str) else value
        for key, value in values.items()
    }


def apply_curriculum_manifest(connection: Connection, manifest: CurriculumManifest) -> bool:
    """Apply a versioned curriculum DAG manifest once, rejecting changed same-version data."""
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

    subject_values = _uuid_values(dict(manifest.payload["subject"]))
    subject_id = subject_values.pop("id")
    _upsert(connection, subjects, {"id": subject_id}, subject_values)

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
