"""Deliberate-memory repository port and SQLAlchemy Core implementation.

Every owner-scoped read and write requires the resolved owner id as a positional
scope argument; the repository never accepts an unverified request-body owner.
Writes are performed on the unit-of-work connection so a capture, its provenance
source, graph-sync state, audit, and outbox job all commit or roll back as one
atomic transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, and_, or_, select

from app.domain.memory import (
    AutoSaveRule,
    Consent,
    ConsentStatus,
    GraphSyncState,
    GraphSyncStatus,
    MemoryEpisode,
    EpisodeStatus,
    Source,
    SourceChunk,
    SourceStatus,
)
from app.persistence.models import (
    auto_save_rules,
    consents,
    graph_sync_state,
    memory_episodes,
    memory_retrieval_log,
    require_owner,
    source_chunks,
    sources,
    utc_datetime,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MemoryRepository(Protocol):
    """Port for deliberate-memory persistence used by the memory service."""

    # -- sources -----------------------------------------------------------
    def add_source(self, source: Source) -> Source: ...

    def get_source(self, owner_user_id: UUID, source_id: UUID) -> Source | None: ...

    def set_source_status(
        self, owner_user_id: UUID, source_id: UUID, status: SourceStatus, *, at: datetime
    ) -> Source | None: ...

    # -- episodes ----------------------------------------------------------
    def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode: ...

    def get_episode(self, owner_user_id: UUID, episode_id: UUID) -> MemoryEpisode | None: ...

    # -- source chunks (owner-filtered vector retrieval) -------------------
    def search_source_chunks(
        self,
        owner_user_id: UUID,
        *,
        subject_ids: frozenset[UUID] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 40,
    ) -> list[SourceChunk]: ...

    def log_retrieval(
        self,
        owner_user_id: UUID,
        *,
        query_hash: str,
        filter_hash: str,
        result_ids: list[str],
        retrieved_at: datetime,
    ) -> None: ...

    # -- graph sync state --------------------------------------------------
    def add_graph_sync_state(self, state: GraphSyncState) -> GraphSyncState: ...

    def get_graph_sync_state(
        self, owner_user_id: UUID, episode_id: UUID
    ) -> GraphSyncState | None: ...

    def record_graph_sync_failure(
        self, owner_user_id: UUID, episode_id: UUID, error_code: str, at: datetime
    ) -> GraphSyncState | None: ...

    def mark_graph_sync_synced(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> GraphSyncState | None: ...

    def mark_graph_sync_retracted(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> GraphSyncState | None: ...

    # -- consents ----------------------------------------------------------
    def add_consent(self, consent: Consent) -> Consent: ...

    def get_consent(self, owner_user_id: UUID, consent_id: UUID) -> Consent | None: ...

    def find_consent(
        self, owner_user_id: UUID, kind: str, policy_version: str
    ) -> Consent | None: ...

    def set_consent_status(
        self,
        owner_user_id: UUID,
        consent_id: UUID,
        status: ConsentStatus,
        *,
        at: datetime,
    ) -> Consent | None: ...

    # -- auto-save rules ---------------------------------------------------
    def add_auto_save_rule(self, rule: AutoSaveRule) -> AutoSaveRule: ...

    def find_auto_save_rule_by_name(
        self, owner_user_id: UUID, name: str
    ) -> AutoSaveRule | None: ...


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _source_from_row(row: Any) -> Source:
    return Source(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        kind=row.kind,
        title=row.title,
        uri=row.uri,
        content_checksum=row.content_checksum,
        provenance=dict(row.provenance or {}),
        status=SourceStatus(row.status),
        deleted_at=_as_utc(row.deleted_at),
    )


def _episode_from_row(row: Any) -> MemoryEpisode:
    return MemoryEpisode(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        source_id=row.source_id,
        kind=row.kind,
        content=row.content,
        visibility=row.visibility,
        user_confidence=row.user_confidence,
        status=EpisodeStatus(row.status),
    )


def _coerce_embedding(value: Any) -> tuple[float, ...] | None:
    """Coerce a stored pgvector value into a plain float tuple.

    pgvector round-trips as a numpy array under PostgreSQL and SQLite alike;
    JSON/text fallbacks and ``None`` are handled defensively so ranking never
    depends on the storage representation.
    """
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    try:
        return tuple(float(component) for component in value)
    except (TypeError, ValueError):
        return None


def _source_chunk_from_row(row: Any) -> SourceChunk:
    return SourceChunk(
        id=row.id,
        owner_user_id=row.owner_user_id,
        subject_id=row.subject_id,
        source_id=row.source_id,
        episode_id=row.episode_id,
        position=row.position,
        content=row.content,
        embedding=_coerce_embedding(row.embedding),
        metadata=dict(row.metadata_json or {}),
        user_confidence=row.user_confidence,
        created_at=_as_utc(row.created_at) or row.created_at,
    )


def _graph_sync_from_row(row: Any) -> GraphSyncState:
    return GraphSyncState(
        id=row.id,
        owner_user_id=row.owner_user_id,
        episode_id=row.episode_id,
        status=GraphSyncStatus(row.status),
        graph_group=row.graph_group,
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        synced_at=_as_utc(row.synced_at),
    )


def _consent_from_row(row: Any) -> Consent:
    return Consent(
        id=row.id,
        owner_user_id=row.owner_user_id,
        kind=row.kind,
        status=ConsentStatus(row.status),
        policy_version=row.policy_version,
        granted_at=_as_utc(row.granted_at),
        revoked_at=_as_utc(row.revoked_at),
    )


def _rule_from_row(row: Any) -> AutoSaveRule:
    return AutoSaveRule(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        source_kind=row.source_kind,
        consent_id=row.consent_id,
        enabled=bool(row.enabled),
        rule_json=dict(row.rule_json or {}),
    )


class SqlMemoryRepository(MemoryRepository):
    """SQLAlchemy Core implementation backed by a live (unit-of-work) connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    # -- sources -----------------------------------------------------------
    def add_source(self, source: Source) -> Source:
        owner = require_owner(source.owner_user_id)
        self._connection.execute(
            sources.insert().values(
                id=source.id,
                owner_user_id=owner,
                subject_id=source.subject_id,
                kind=source.kind,
                title=source.title,
                uri=source.uri,
                content_checksum=source.content_checksum,
                provenance=dict(source.provenance),
                status=source.status.value,
                deleted_at=source.deleted_at,
            )
        )
        stored = self.get_source(owner, source.id)
        assert stored is not None
        return stored

    def get_source(self, owner_user_id: UUID, source_id: UUID) -> Source | None:
        row = self._connection.execute(
            select(sources).where(
                and_(sources.c.owner_user_id == owner_user_id, sources.c.id == source_id)
            )
        ).first()
        return _source_from_row(row) if row is not None else None

    def set_source_status(
        self, owner_user_id: UUID, source_id: UUID, status: SourceStatus, *, at: datetime
    ) -> Source | None:
        deleted_at = utc_datetime(at) if status is SourceStatus.DELETED else None
        result = self._connection.execute(
            sources.update()
            .where(and_(sources.c.owner_user_id == owner_user_id, sources.c.id == source_id))
            .values(status=status.value, deleted_at=deleted_at, updated_at=utc_datetime(at))
        )
        if result.rowcount == 0:
            return None
        return self.get_source(owner_user_id, source_id)

    # -- episodes ----------------------------------------------------------
    def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode:
        owner = require_owner(episode.owner_user_id)
        self._connection.execute(
            memory_episodes.insert().values(
                id=episode.id,
                owner_user_id=owner,
                subject_id=episode.subject_id,
                source_id=episode.source_id,
                kind=episode.kind,
                content=episode.content,
                visibility=episode.visibility,
                user_confidence=episode.user_confidence,
                status=episode.status.value,
            )
        )
        stored = self.get_episode(owner, episode.id)
        assert stored is not None
        return stored

    def get_episode(self, owner_user_id: UUID, episode_id: UUID) -> MemoryEpisode | None:
        row = self._connection.execute(
            select(memory_episodes).where(
                and_(
                    memory_episodes.c.owner_user_id == owner_user_id,
                    memory_episodes.c.id == episode_id,
                )
            )
        ).first()
        return _episode_from_row(row) if row is not None else None

    # -- source chunks (owner-filtered vector retrieval) -------------------
    def search_source_chunks(
        self,
        owner_user_id: UUID,
        *,
        subject_ids: frozenset[UUID] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 40,
    ) -> list[SourceChunk]:
        """Return live, owner-scoped candidate chunks before similarity ranking.

        The authenticated owner, permitted-subject, requested-date, and
        live-lifecycle predicates are all applied in the query, so ranking only
        ever sees authorized rows and an exact filter that matches nothing
        returns an empty list without broadening scope (Requirements 9.7, 9.8,
        10.3).
        """
        owner = require_owner(owner_user_id)
        if subject_ids is not None and len(subject_ids) == 0:
            # An explicit empty permitted-subject set matches nothing; never
            # broaden to all subjects.
            return []

        conditions = [
            source_chunks.c.owner_user_id == owner,
            source_chunks.c.deleted_at.is_(None),
            sources.c.status == SourceStatus.ACTIVE.value,
            sources.c.deleted_at.is_(None),
            or_(
                memory_episodes.c.id.is_(None),
                and_(
                    memory_episodes.c.status == EpisodeStatus.ACTIVE.value,
                    memory_episodes.c.deleted_at.is_(None),
                ),
            ),
        ]
        if subject_ids is not None:
            conditions.append(source_chunks.c.subject_id.in_(tuple(subject_ids)))
        if date_from is not None:
            conditions.append(source_chunks.c.created_at >= utc_datetime(date_from))
        if date_to is not None:
            conditions.append(source_chunks.c.created_at <= utc_datetime(date_to))

        statement = (
            select(
                source_chunks,
                memory_episodes.c.user_confidence.label("user_confidence"),
            )
            .select_from(
                source_chunks.join(sources, source_chunks.c.source_id == sources.c.id).outerjoin(
                    memory_episodes, source_chunks.c.episode_id == memory_episodes.c.id
                )
            )
            .where(and_(*conditions))
            .order_by(source_chunks.c.created_at.desc(), source_chunks.c.position.asc())
            .limit(limit)
        )
        rows = self._connection.execute(statement).all()
        return [_source_chunk_from_row(row) for row in rows]

    def log_retrieval(
        self,
        owner_user_id: UUID,
        *,
        query_hash: str,
        filter_hash: str,
        result_ids: list[str],
        retrieved_at: datetime,
    ) -> None:
        """Persist a retrieval log carrying only hashes, IDs, and counts."""
        owner = require_owner(owner_user_id)
        self._connection.execute(
            memory_retrieval_log.insert().values(
                id=uuid4(),
                owner_user_id=owner,
                query_hash=query_hash,
                filter_hash=filter_hash,
                result_ids=list(result_ids),
                result_count=len(result_ids),
                retrieved_at=utc_datetime(retrieved_at),
            )
        )

    # -- graph sync state --------------------------------------------------
    def add_graph_sync_state(self, state: GraphSyncState) -> GraphSyncState:
        owner = require_owner(state.owner_user_id)
        self._connection.execute(
            graph_sync_state.insert().values(
                id=state.id,
                owner_user_id=owner,
                episode_id=state.episode_id,
                status=state.status.value,
                graph_group=state.graph_group,
                attempt_count=state.attempt_count,
                last_error_code=state.last_error_code,
                synced_at=state.synced_at,
            )
        )
        stored = self.get_graph_sync_state(owner, state.episode_id)
        assert stored is not None
        return stored

    def get_graph_sync_state(
        self, owner_user_id: UUID, episode_id: UUID
    ) -> GraphSyncState | None:
        row = self._connection.execute(
            select(graph_sync_state).where(
                and_(
                    graph_sync_state.c.owner_user_id == owner_user_id,
                    graph_sync_state.c.episode_id == episode_id,
                )
            )
        ).first()
        return _graph_sync_from_row(row) if row is not None else None

    def record_graph_sync_failure(
        self, owner_user_id: UUID, episode_id: UUID, error_code: str, at: datetime
    ) -> GraphSyncState | None:
        current = self.get_graph_sync_state(owner_user_id, episode_id)
        if current is None:
            return None
        self._connection.execute(
            graph_sync_state.update()
            .where(
                and_(
                    graph_sync_state.c.owner_user_id == owner_user_id,
                    graph_sync_state.c.episode_id == episode_id,
                )
            )
            .values(
                status=GraphSyncStatus.FAILED.value,
                attempt_count=current.attempt_count + 1,
                last_error_code=error_code,
                updated_at=utc_datetime(at),
            )
        )
        return self.get_graph_sync_state(owner_user_id, episode_id)

    def mark_graph_sync_synced(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> GraphSyncState | None:
        result = self._connection.execute(
            graph_sync_state.update()
            .where(
                and_(
                    graph_sync_state.c.owner_user_id == owner_user_id,
                    graph_sync_state.c.episode_id == episode_id,
                )
            )
            .values(
                status=GraphSyncStatus.SYNCED.value,
                last_error_code=None,
                synced_at=utc_datetime(at),
                updated_at=utc_datetime(at),
            )
        )
        if result.rowcount == 0:
            return None
        return self.get_graph_sync_state(owner_user_id, episode_id)

    def mark_graph_sync_retracted(
        self, owner_user_id: UUID, episode_id: UUID, at: datetime
    ) -> GraphSyncState | None:
        result = self._connection.execute(
            graph_sync_state.update()
            .where(
                and_(
                    graph_sync_state.c.owner_user_id == owner_user_id,
                    graph_sync_state.c.episode_id == episode_id,
                )
            )
            .values(
                status=GraphSyncStatus.RETRACTED.value,
                last_error_code=None,
                updated_at=utc_datetime(at),
            )
        )
        if result.rowcount == 0:
            return None
        return self.get_graph_sync_state(owner_user_id, episode_id)

    # -- consents ----------------------------------------------------------
    def add_consent(self, consent: Consent) -> Consent:
        owner = require_owner(consent.owner_user_id)
        self._connection.execute(
            consents.insert().values(
                id=consent.id,
                owner_user_id=owner,
                kind=consent.kind,
                status=consent.status.value,
                policy_version=consent.policy_version,
                granted_at=consent.granted_at,
                revoked_at=consent.revoked_at,
            )
        )
        stored = self.get_consent(owner, consent.id)
        assert stored is not None
        return stored

    def get_consent(self, owner_user_id: UUID, consent_id: UUID) -> Consent | None:
        row = self._connection.execute(
            select(consents).where(
                and_(consents.c.owner_user_id == owner_user_id, consents.c.id == consent_id)
            )
        ).first()
        return _consent_from_row(row) if row is not None else None

    def find_consent(
        self, owner_user_id: UUID, kind: str, policy_version: str
    ) -> Consent | None:
        row = self._connection.execute(
            select(consents).where(
                and_(
                    consents.c.owner_user_id == owner_user_id,
                    consents.c.kind == kind,
                    consents.c.policy_version == policy_version,
                )
            )
        ).first()
        return _consent_from_row(row) if row is not None else None

    def set_consent_status(
        self,
        owner_user_id: UUID,
        consent_id: UUID,
        status: ConsentStatus,
        *,
        at: datetime,
    ) -> Consent | None:
        values: dict[str, Any] = {"status": status.value, "updated_at": utc_datetime(at)}
        if status is ConsentStatus.GRANTED:
            values["granted_at"] = utc_datetime(at)
            values["revoked_at"] = None
        elif status is ConsentStatus.REVOKED:
            values["revoked_at"] = utc_datetime(at)
        result = self._connection.execute(
            consents.update()
            .where(
                and_(consents.c.owner_user_id == owner_user_id, consents.c.id == consent_id)
            )
            .values(**values)
        )
        if result.rowcount == 0:
            return None
        return self.get_consent(owner_user_id, consent_id)

    # -- auto-save rules ---------------------------------------------------
    def add_auto_save_rule(self, rule: AutoSaveRule) -> AutoSaveRule:
        owner = require_owner(rule.owner_user_id)
        self._connection.execute(
            auto_save_rules.insert().values(
                id=rule.id,
                owner_user_id=owner,
                name=rule.name,
                source_kind=rule.source_kind,
                consent_id=rule.consent_id,
                enabled=rule.enabled,
                rule_json=dict(rule.rule_json),
            )
        )
        stored = self.find_auto_save_rule_by_name(owner, rule.name)
        assert stored is not None
        return stored

    def find_auto_save_rule_by_name(
        self, owner_user_id: UUID, name: str
    ) -> AutoSaveRule | None:
        row = self._connection.execute(
            select(auto_save_rules).where(
                and_(
                    auto_save_rules.c.owner_user_id == owner_user_id,
                    auto_save_rules.c.name == name,
                )
            )
        ).first()
        return _rule_from_row(row) if row is not None else None
