"""Deliberate memory, consent, auto-save, upload-quarantine, and provenance service.

The service captures learner context only on explicit save intent or a matching
named auto-save rule backed by a granted consent. Ordinary chat never becomes a
long-term ``Memory_Episode`` (Requirement 9.3). Every accepted capture records a
``Source_Record`` with provenance, an episode, a pending graph-sync state, an
audit record, and a durable outbox job in one atomic transaction, so graph
ingestion can be retried without ever losing the canonical episode
(Requirements 9.1, 9.5, 9.10). Canonical episodes remain authoritative over
graph augmentation (Requirement 9.6).

Uploads are validated for size and type and screened for malware before
ingestion; anything unclean is quarantined and rejected without executing the
imported content (Requirements 19.11, 19.12, 19.13).

The effective owner scope is always taken from the authenticated
:class:`ActorContext`; a client-supplied owner identifier is never trusted, and
observers may not mutate learner memory.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Protocol, Sequence
from uuid import UUID, uuid4

from app.clock import Clock, system_clock
from app.domain.identity import ActorContext, AuthorizationError
from app.domain.memory import (
    AutoSaveRule,
    AutoSaveRuleError,
    CaptureTrigger,
    CapturedMemory,
    ChatCaptureResult,
    Consent,
    ConsentRequiredError,
    ConsentStatus,
    EpisodeKind,
    EpisodeStatus,
    FileScanner,
    FileUpload,
    GraphSyncState,
    GraphSyncStatus,
    MemoryEpisode,
    MemoryValidationError,
    Source,
    SourceChunk,
    SourceKind,
    SourceStatus,
    UploadLimits,
    UploadRejectedError,
    Visibility,
    build_provenance,
    coerce_episode_kind,
    compute_checksum,
    decide_capture,
    graph_group,
    normalize_content,
    validate_confidence,
    validate_upload,
)
from app.domain.retrieval import (
    EvidenceKind,
    GraphRetrievalPort,
    RankingWeights,
    RetrievalContext,
    RetrievalFilters,
    RetrievalItem,
    RetrievalLimits,
    SupplementaryUnavailableError,
    UnavailableGraphRetrieval,
    assemble_context,
    cosine_similarity,
    make_item,
)
from app.repositories.idempotency import hash_request
from app.repositories.memory import MemoryRepository, SqlMemoryRepository

SAVE_OPERATION = "memory.save"
GRAPH_INGESTION_JOB = "graph_ingestion"


class UnitOfWorkLike(Protocol):
    """The transactional surface the memory service relies on."""

    connection: Any
    idempotency: Any
    audit: Any
    outbox: Any

    def __enter__(self) -> "UnitOfWorkLike": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...


def _now(at: datetime | None = None) -> datetime:
    return at if at is not None else datetime.now(timezone.utc)


class MemoryService:
    """Use cases for deliberate memory capture, consent, and provenance."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWorkLike],
        *,
        scanner: FileScanner,
        repository_factory: Callable[[Any], MemoryRepository] = SqlMemoryRepository,
        clock: Clock = system_clock,
        limits: UploadLimits | None = None,
        graph_retrieval: GraphRetrievalPort | None = None,
        retrieval_limits: RetrievalLimits | None = None,
        ranking_weights: RankingWeights | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._scanner = scanner
        self._repository_factory = repository_factory
        self._clock = clock
        self._limits = limits or UploadLimits()
        # Graph retrieval defaults to the unavailable adapter so local operation
        # degrades safely until the Graphiti adapter (task 6.3) is wired in.
        self._graph_retrieval = graph_retrieval or UnavailableGraphRetrieval()
        self._retrieval_limits = retrieval_limits or RetrievalLimits()
        self._ranking_weights = ranking_weights or RankingWeights()

    # -- explicit save -----------------------------------------------------
    def save_context(
        self,
        actor: ActorContext,
        *,
        content: str,
        kind: str | EpisodeKind,
        subject_id: UUID | None = None,
        source_kind: SourceKind | str = SourceKind.MANUAL_ENTRY,
        source_title: str | None = None,
        source_uri: str | None = None,
        visibility: Visibility | str = Visibility.PRIVATE,
        user_confidence: float | None = None,
        upload: FileUpload | None = None,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> CapturedMemory:
        """Capture explicit-save content, its provenance source, and graph job.

        Empty content raises a validation error before anything is written
        (Requirement 9.11). A supplied upload is validated and screened before
        ingestion; an unclean upload is quarantined and rejected (Requirements
        19.11, 19.12, 19.13).
        """
        self._require_owner(actor)
        clean = normalize_content(content)
        episode_kind = coerce_episode_kind(kind)
        confidence = validate_confidence(user_confidence)
        captured_at = _now(at)

        resolved_source_kind = SourceKind(str(source_kind))
        untrusted = False
        checksum = compute_checksum(clean)
        if upload is not None:
            self._screen_upload(actor, upload, subject_id, captured_at)
            resolved_source_kind = SourceKind.UPLOADED_FILE
            untrusted = True
            checksum = compute_checksum(upload.content)
            if source_title is None:
                source_title = upload.filename

        return self._capture(
            actor,
            trigger=CaptureTrigger.EXPLICIT_SAVE,
            content=clean,
            episode_kind=episode_kind,
            subject_id=subject_id,
            source_kind=resolved_source_kind,
            source_title=source_title,
            source_uri=source_uri,
            visibility=Visibility(str(visibility)),
            confidence=confidence,
            checksum=checksum,
            untrusted=untrusted,
            rule_name=None,
            captured_at=captured_at,
            idempotency_key=idempotency_key,
        )

    # -- auto-save rule capture -------------------------------------------
    def capture_by_rule(
        self,
        actor: ActorContext,
        *,
        rule_name: str,
        content: str,
        kind: str | EpisodeKind = EpisodeKind.RESOURCE,
        subject_id: UUID | None = None,
        source_kind: SourceKind | str | None = None,
        source_title: str | None = None,
        source_uri: str | None = None,
        visibility: Visibility | str = Visibility.PRIVATE,
        user_confidence: float | None = None,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> CapturedMemory:
        """Capture only content covered by a named rule and consent (Requirement 9.2)."""
        self._require_owner(actor)
        clean = normalize_content(content)
        episode_kind = coerce_episode_kind(kind)
        confidence = validate_confidence(user_confidence)
        captured_at = _now(at)

        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            rule = self._require_matching_rule(repo, actor.owner_id, rule_name, source_kind)
            resolved_source_kind = SourceKind(rule.source_kind)
            return self._capture(
                actor,
                trigger=CaptureTrigger.AUTO_SAVE_RULE,
                content=clean,
                episode_kind=episode_kind,
                subject_id=subject_id,
                source_kind=resolved_source_kind,
                source_title=source_title,
                source_uri=source_uri,
                visibility=Visibility(str(visibility)),
                confidence=confidence,
                checksum=compute_checksum(clean),
                untrusted=True,
                rule_name=rule.name,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
                uow=uow,
                repo=repo,
            )

    # -- chat turn boundary (Requirement 9.3) -----------------------------
    def capture_chat_turn(
        self,
        actor: ActorContext,
        *,
        content: str,
        explicit_save: bool = False,
        rule_name: str | None = None,
        kind: str | EpisodeKind = EpisodeKind.CONVERSATION_SUMMARY,
        subject_id: UUID | None = None,
        at: datetime | None = None,
    ) -> ChatCaptureResult:
        """Offer a chat turn to memory; persist only on explicit save or a rule.

        With neither explicit save intent nor a matching enabled auto-save rule,
        the turn is deliberately kept out of long-term memory.
        """
        self._require_owner(actor)
        if explicit_save:
            captured = self.save_context(
                actor,
                content=content,
                kind=kind,
                subject_id=subject_id,
                source_kind=SourceKind.CHAT_SUMMARY,
                at=at,
            )
            return ChatCaptureResult(decide_capture(explicit_save=True, matching_rule=False), captured)

        if rule_name is not None and self._rule_matches(actor, rule_name, SourceKind.CHAT_SUMMARY):
            captured = self.capture_by_rule(
                actor,
                rule_name=rule_name,
                content=content,
                kind=kind,
                subject_id=subject_id,
                source_kind=SourceKind.CHAT_SUMMARY,
                at=at,
            )
            return ChatCaptureResult(decide_capture(explicit_save=False, matching_rule=True), captured)

        return ChatCaptureResult(decide_capture(explicit_save=False, matching_rule=False), None)

    # -- graph sync lifecycle (Requirement 9.10) --------------------------
    def record_graph_sync_failure(
        self,
        actor: ActorContext,
        episode_id: UUID,
        *,
        error_code: str,
        at: datetime | None = None,
    ) -> GraphSyncState:
        """Record a failed graph ingestion while retaining the canonical episode.

        The accepted local episode is untouched, the sync state moves to
        ``failed`` with an incremented attempt count, and the outbox job remains
        eligible for retry under the worker policy.
        """
        self._require_owner(actor)
        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            episode = repo.get_episode(actor.owner_id, episode_id)
            if episode is None or episode.status is not EpisodeStatus.ACTIVE:
                raise MemoryValidationError("Unknown episode in this scope.", field="episode_id")
            state = repo.record_graph_sync_failure(
                actor.owner_id, episode_id, error_code, _now(at)
            )
            if state is None:
                raise MemoryValidationError("No graph sync state for this episode.", field="episode_id")
            uow.audit.record(
                owner_user_id=actor.owner_id,
                actor_user_id=actor.actor_id,
                action="memory.graph_sync_failed",
                resource_kind="memory_episode",
                resource_id=episode_id,
                outcome="failed",
                details={"error_code": error_code, "attempt_count": state.attempt_count},
            )
            uow.commit()
            return state

    def mark_graph_synced(
        self, actor: ActorContext, episode_id: UUID, *, at: datetime | None = None
    ) -> GraphSyncState:
        """Mark a previously accepted episode as successfully ingested into the graph."""
        self._require_owner(actor)
        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            state = repo.mark_graph_sync_synced(actor.owner_id, episode_id, _now(at))
            if state is None:
                raise MemoryValidationError("No graph sync state for this episode.", field="episode_id")
            uow.commit()
            return state

    def get_graph_sync_state(self, actor: ActorContext, episode_id: UUID) -> GraphSyncState | None:
        with self._read_repo() as repo:
            return repo.get_graph_sync_state(actor.owner_id, episode_id)

    # -- grounded retrieval (Requirements 9.7, 9.8, 10.2-10.6, 10.9, 10.10) -
    def retrieve_context(
        self,
        actor: ActorContext,
        *,
        query: str,
        query_embedding: Sequence[float] | None = None,
        canonical: Sequence[RetrievalItem] = (),
        filters: RetrievalFilters | None = None,
        limits: RetrievalLimits | None = None,
        at: datetime | None = None,
        log: bool = True,
    ) -> RetrievalContext:
        """Assemble a bounded, deduplicated, deterministically ranked context.

        Canonical evidence supplied by the caller is ranked ahead of the
        owner-filtered source chunks and permitted Graph_Memory facts retrieved
        here (Requirement 10.2). The repository applies the authenticated owner,
        permitted-subject, date, and live-lifecycle filters before similarity
        ranking, so an exact filter that matches nothing yields an empty context
        without broadening scope (Requirements 9.7, 9.8, 10.3, 10.9). A failed
        vector or graph path degrades to an explicit unavailable status rather
        than presenting unsupported claims as grounded (Requirement 10.10), and
        the result is bounded by configured record and token limits before any
        AI request (Requirements 10.6, 22.4).
        """
        self._require_memory_read(actor)
        active_filters = filters or RetrievalFilters()
        active_limits = limits or self._retrieval_limits
        now = _now(at)

        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)

            vector_available, source_items = self._retrieve_source_chunks(
                repo, actor.owner_id, query_embedding, active_filters, active_limits
            )
            graph_available, graph_items = self._retrieve_graph_facts(
                actor.owner_id, query, active_filters, active_limits
            )

            context = assemble_context(
                canonical=tuple(canonical),
                source_chunks=source_items,
                graph_facts=graph_items,
                limits=active_limits,
                weights=self._ranking_weights,
                vector_available=vector_available,
                graph_available=graph_available,
            )

            if log:
                repo.log_retrieval(
                    actor.owner_id,
                    query_hash=compute_checksum(query),
                    filter_hash=self._filter_hash(active_filters),
                    result_ids=[item.ref_id for item in context.items],
                    retrieved_at=now,
                )
                uow.commit()

            return context

    def _retrieve_source_chunks(
        self,
        repo: MemoryRepository,
        owner_id: UUID,
        query_embedding: Sequence[float] | None,
        filters: RetrievalFilters,
        limits: RetrievalLimits,
    ) -> tuple[bool, list[RetrievalItem]]:
        """Fetch owner-filtered chunks and score them by similarity."""
        try:
            chunks = repo.search_source_chunks(
                owner_id,
                subject_ids=filters.permitted_subject_ids,
                date_from=filters.date_from,
                date_to=filters.date_to,
                limit=limits.candidate_limit,
            )
        except Exception:  # pragma: no cover - supplementary path degrades safely
            return False, []
        return True, [self._chunk_to_item(chunk, query_embedding, filters) for chunk in chunks]

    def _chunk_to_item(
        self,
        chunk: SourceChunk,
        query_embedding: Sequence[float] | None,
        filters: RetrievalFilters,
    ) -> RetrievalItem:
        relevance = 0.0
        if query_embedding is not None and chunk.embedding is not None:
            relevance = cosine_similarity(query_embedding, chunk.embedding)
        subject_match = (
            filters.focus_subject_id is not None
            and chunk.subject_id == filters.focus_subject_id
        )
        return make_item(
            kind=EvidenceKind.SOURCE_CHUNK,
            ref_id=str(chunk.id),
            dedup_key=f"source:{chunk.source_id}:{chunk.position}",
            content=chunk.content,
            relevance=relevance,
            recorded_at=chunk.created_at,
            subject_id=chunk.subject_id,
            subject_match=subject_match,
            graph_related=False,
            user_confidence=chunk.user_confidence,
            provenance={
                "source_id": str(chunk.source_id),
                "episode_id": str(chunk.episode_id) if chunk.episode_id is not None else None,
                "position": chunk.position,
                **chunk.metadata,
            },
        )

    def _retrieve_graph_facts(
        self,
        owner_id: UUID,
        query: str,
        filters: RetrievalFilters,
        limits: RetrievalLimits,
    ) -> tuple[bool, list[RetrievalItem]]:
        """Retrieve permitted Graph_Memory facts, degrading safely on failure."""
        if limits.max_graph_facts <= 0:
            return True, []
        groups = self._permitted_graph_groups(owner_id, filters)
        try:
            facts = self._graph_retrieval.search_facts(
                owner_user_id=owner_id,
                groups=groups,
                query=query,
                limit=limits.max_graph_facts,
            )
        except SupplementaryUnavailableError:
            return False, []
        except Exception:  # pragma: no cover - any graph outage degrades safely
            return False, []
        return True, list(facts)

    @staticmethod
    def _permitted_graph_groups(owner_id: UUID, filters: RetrievalFilters) -> list[str]:
        """Derive the exact owner/subject groups permitted for graph retrieval.

        With no subject restriction the owner-level group is used; an explicit
        permitted-subject set restricts retrieval to exactly those subject
        groups so scope is never broadened (Requirement 10.4).
        """
        if filters.permitted_subject_ids is None:
            return [graph_group(owner_id, None)]
        return [graph_group(owner_id, subject_id) for subject_id in sorted(filters.permitted_subject_ids, key=str)]

    @staticmethod
    def _filter_hash(filters: RetrievalFilters) -> str:
        subjects = (
            None
            if filters.permitted_subject_ids is None
            else sorted(str(subject) for subject in filters.permitted_subject_ids)
        )
        return hash_request(
            {
                "permitted_subject_ids": subjects,
                "date_from": filters.date_from.isoformat() if filters.date_from else None,
                "date_to": filters.date_to.isoformat() if filters.date_to else None,
                "focus_subject_id": str(filters.focus_subject_id)
                if filters.focus_subject_id is not None
                else None,
            }
        )

    def _require_memory_read(self, actor: ActorContext) -> None:
        """Retrieval requires learner ownership or a granted memory-read scope."""
        if actor.is_owner or actor.has_scope("memory:read"):
            return
        raise AuthorizationError("This action requires memory read access.")

    # -- consent management -----------------------------------------------
    def record_consent(
        self,
        actor: ActorContext,
        *,
        kind: str,
        policy_version: str,
        at: datetime | None = None,
    ) -> Consent:
        """Record (or re-grant) a consent scoped to the authenticated owner."""
        self._require_owner(actor)
        clean_kind = (kind or "").strip()
        if not clean_kind:
            raise MemoryValidationError("Consent kind is required.", field="kind")
        clean_version = (policy_version or "").strip()
        if not clean_version:
            raise MemoryValidationError("Policy version is required.", field="policy_version")
        now = _now(at)
        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            existing = repo.find_consent(actor.owner_id, clean_kind, clean_version)
            if existing is not None:
                stored = repo.set_consent_status(
                    actor.owner_id, existing.id, ConsentStatus.GRANTED, at=now
                )
            else:
                stored = repo.add_consent(
                    Consent(
                        id=uuid4(),
                        owner_user_id=actor.owner_id,
                        kind=clean_kind,
                        status=ConsentStatus.GRANTED,
                        policy_version=clean_version,
                        granted_at=now,
                        revoked_at=None,
                    )
                )
            assert stored is not None
            uow.commit()
            return stored

    def revoke_consent(
        self, actor: ActorContext, consent_id: UUID, *, at: datetime | None = None
    ) -> Consent:
        self._require_owner(actor)
        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            stored = repo.set_consent_status(
                actor.owner_id, consent_id, ConsentStatus.REVOKED, at=_now(at)
            )
            if stored is None:
                raise MemoryValidationError("Unknown consent in this scope.", field="consent_id")
            uow.commit()
            return stored

    # -- auto-save rule management ----------------------------------------
    def create_auto_save_rule(
        self,
        actor: ActorContext,
        *,
        name: str,
        source_kind: SourceKind | str,
        consent_id: UUID,
        rule_json: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> AutoSaveRule:
        """Create a named auto-save rule that must reference a granted consent."""
        self._require_owner(actor)
        clean_name = (name or "").strip()
        if not clean_name:
            raise MemoryValidationError("Rule name is required.", field="name")
        try:
            resolved_kind = SourceKind(str(source_kind))
        except ValueError as error:
            raise MemoryValidationError("Unsupported source kind.", field="source_kind") from error

        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            if repo.find_auto_save_rule_by_name(actor.owner_id, clean_name) is not None:
                raise MemoryValidationError("A rule with this name already exists.", field="name")
            consent = repo.get_consent(actor.owner_id, consent_id)
            if consent is None:
                raise MemoryValidationError("Unknown consent in this scope.", field="consent_id")
            if not consent.is_granted:
                raise ConsentRequiredError("The referenced consent is not granted.")
            stored = repo.add_auto_save_rule(
                AutoSaveRule(
                    id=uuid4(),
                    owner_user_id=actor.owner_id,
                    name=clean_name,
                    source_kind=resolved_kind.value,
                    consent_id=consent_id,
                    enabled=enabled,
                    rule_json=dict(rule_json or {}),
                )
            )
            uow.commit()
            return stored

    # -- internal capture --------------------------------------------------
    def _capture(
        self,
        actor: ActorContext,
        *,
        trigger: CaptureTrigger,
        content: str,
        episode_kind: EpisodeKind,
        subject_id: UUID | None,
        source_kind: SourceKind,
        source_title: str | None,
        source_uri: str | None,
        visibility: Visibility,
        confidence: float | None,
        checksum: str,
        untrusted: bool,
        rule_name: str | None,
        captured_at: datetime,
        idempotency_key: str | None,
        uow: UnitOfWorkLike | None = None,
        repo: MemoryRepository | None = None,
    ) -> CapturedMemory:
        """Perform the atomic capture transaction shared by every entry point."""
        if uow is not None and repo is not None:
            return self._capture_within(
                actor,
                uow,
                repo,
                trigger=trigger,
                content=content,
                episode_kind=episode_kind,
                subject_id=subject_id,
                source_kind=source_kind,
                source_title=source_title,
                source_uri=source_uri,
                visibility=visibility,
                confidence=confidence,
                checksum=checksum,
                untrusted=untrusted,
                rule_name=rule_name,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
            )
        with self._uow_factory() as new_uow:
            new_repo = self._repository_factory(new_uow.connection)
            return self._capture_within(
                actor,
                new_uow,
                new_repo,
                trigger=trigger,
                content=content,
                episode_kind=episode_kind,
                subject_id=subject_id,
                source_kind=source_kind,
                source_title=source_title,
                source_uri=source_uri,
                visibility=visibility,
                confidence=confidence,
                checksum=checksum,
                untrusted=untrusted,
                rule_name=rule_name,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
            )

    def _capture_within(
        self,
        actor: ActorContext,
        uow: UnitOfWorkLike,
        repo: MemoryRepository,
        *,
        trigger: CaptureTrigger,
        content: str,
        episode_kind: EpisodeKind,
        subject_id: UUID | None,
        source_kind: SourceKind,
        source_title: str | None,
        source_uri: str | None,
        visibility: Visibility,
        confidence: float | None,
        checksum: str,
        untrusted: bool,
        rule_name: str | None,
        captured_at: datetime,
        idempotency_key: str | None,
    ) -> CapturedMemory:
        if idempotency_key is not None:
            request_hash = hash_request(
                {
                    "content_checksum": checksum,
                    "kind": episode_kind.value,
                    "subject_id": str(subject_id) if subject_id is not None else None,
                    "trigger": trigger.value,
                }
            )
            claim = uow.idempotency.begin(
                owner_user_id=actor.owner_id,
                operation=SAVE_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.completed and claim.outcome is not None and claim.outcome.result_ref is not None:
                existing = self._load_captured(repo, actor.owner_id, claim.outcome.result_ref)
                if existing is not None:
                    return existing

        source = repo.add_source(
            Source(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                subject_id=subject_id,
                kind=source_kind.value,
                title=source_title,
                uri=source_uri,
                content_checksum=checksum,
                provenance=build_provenance(
                    source_kind=source_kind,
                    owner_user_id=actor.owner_id,
                    captured_at=captured_at,
                    checksum=checksum,
                    trigger=trigger,
                    origin=source_uri or source_title,
                    subject_id=subject_id,
                    rule_name=rule_name,
                    confidence=confidence,
                    visibility=visibility,
                    untrusted=untrusted,
                ).as_dict(),
                status=SourceStatus.ACTIVE,
            )
        )
        episode = repo.add_episode(
            MemoryEpisode(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                subject_id=subject_id,
                source_id=source.id,
                kind=episode_kind.value,
                content=content,
                visibility=visibility.value,
                user_confidence=confidence,
                status=EpisodeStatus.ACTIVE,
            )
        )
        group = graph_group(actor.owner_id, subject_id)
        graph_state = repo.add_graph_sync_state(
            GraphSyncState(
                id=uuid4(),
                owner_user_id=actor.owner_id,
                episode_id=episode.id,
                status=GraphSyncStatus.PENDING,
                graph_group=group,
                attempt_count=0,
            )
        )
        # The outbox payload carries only identifiers and graph metadata (never
        # full learner content) so a derived fact can cite its supporting
        # episode, source, subject, visibility, creation time, and confidence.
        enqueue = uow.outbox.enqueue(
            owner_user_id=actor.owner_id,
            kind=GRAPH_INGESTION_JOB,
            deduplication_key=f"{GRAPH_INGESTION_JOB}:{episode.id}",
            payload={
                "episode_id": str(episode.id),
                "source_id": str(source.id),
                "graph_group": group,
                "subject_id": str(subject_id) if subject_id is not None else None,
                "visibility": visibility.value,
                "user_confidence": confidence,
                "created_at": captured_at.astimezone(timezone.utc).isoformat(),
            },
        )
        uow.audit.record(
            owner_user_id=actor.owner_id,
            actor_user_id=actor.actor_id,
            action="memory.save",
            resource_kind="memory_episode",
            resource_id=episode.id,
            details={
                "trigger": trigger.value,
                "source_id": str(source.id),
                "rule_name": rule_name,
            },
        )
        if idempotency_key is not None:
            uow.idempotency.complete(
                owner_user_id=actor.owner_id,
                operation=SAVE_OPERATION,
                key=idempotency_key,
                response_status=201,
                result_ref=episode.id,
            )
        uow.commit()
        return CapturedMemory(
            source=source,
            episode=episode,
            graph_sync=graph_state,
            outbox_job_id=enqueue.job_id,
        )

    # -- helpers -----------------------------------------------------------
    def _screen_upload(
        self,
        actor: ActorContext,
        upload: FileUpload,
        subject_id: UUID | None,
        at: datetime,
    ) -> None:
        """Validate and malware-screen an upload, quarantining anything unclean."""
        validate_upload(upload, self._limits)
        result = self._scanner.scan(upload)
        if result.clean:
            return
        self._quarantine_upload(actor, upload, subject_id, result.detail, at)
        raise UploadRejectedError(
            "Uploaded file failed malware screening.",
            reasons=[result.detail or "unsafe_content"],
        )

    def _quarantine_upload(
        self,
        actor: ActorContext,
        upload: FileUpload,
        subject_id: UUID | None,
        detail: str | None,
        at: datetime,
    ) -> None:
        """Persist a quarantined ``Source_Record`` and audit the rejection.

        The untrusted file bytes are never stored or executed; only safe
        metadata (filename, type, checksum, and the screen result) are retained.
        """
        checksum = compute_checksum(upload.content)
        with self._uow_factory() as uow:
            repo = self._repository_factory(uow.connection)
            repo.add_source(
                Source(
                    id=uuid4(),
                    owner_user_id=actor.owner_id,
                    subject_id=subject_id,
                    kind=SourceKind.UPLOADED_FILE.value,
                    title=upload.filename,
                    uri=None,
                    content_checksum=checksum,
                    provenance=build_provenance(
                        source_kind=SourceKind.UPLOADED_FILE,
                        owner_user_id=actor.owner_id,
                        captured_at=at,
                        checksum=checksum,
                        trigger=CaptureTrigger.EXPLICIT_SAVE,
                        origin=upload.filename,
                        subject_id=subject_id,
                        untrusted=True,
                        evidence=(f"screen:{detail or 'unsafe_content'}",),
                    ).as_dict(),
                    status=SourceStatus.QUARANTINED,
                )
            )
            uow.audit.record(
                owner_user_id=actor.owner_id,
                actor_user_id=actor.actor_id,
                action="memory.upload_quarantined",
                resource_kind="source",
                outcome="rejected",
                details={"content_type": upload.content_type, "reason": detail or "unsafe_content"},
            )
            uow.commit()

    def _require_matching_rule(
        self,
        repo: MemoryRepository,
        owner_id: UUID,
        rule_name: str,
        source_kind: SourceKind | str | None,
    ) -> AutoSaveRule:
        rule = repo.find_auto_save_rule_by_name(owner_id, rule_name)
        if rule is None or not rule.enabled:
            raise AutoSaveRuleError(f"No enabled auto-save rule named '{rule_name}'.")
        if source_kind is not None and str(source_kind) != rule.source_kind:
            raise MemoryValidationError(
                "Content is not covered by the named auto-save rule.",
                field="source_kind",
            )
        consent = repo.get_consent(owner_id, rule.consent_id)
        if consent is None or not consent.is_granted:
            raise ConsentRequiredError("The auto-save rule's consent is not granted.")
        return rule

    def _rule_matches(
        self, actor: ActorContext, rule_name: str, source_kind: SourceKind
    ) -> bool:
        with self._read_repo() as repo:
            rule = repo.find_auto_save_rule_by_name(actor.owner_id, rule_name)
            if rule is None or not rule.enabled or rule.source_kind != source_kind.value:
                return False
            consent = repo.get_consent(actor.owner_id, rule.consent_id)
            return consent is not None and consent.is_granted

    def _load_captured(
        self, repo: MemoryRepository, owner_id: UUID, episode_id: UUID
    ) -> CapturedMemory | None:
        episode = repo.get_episode(owner_id, episode_id)
        if episode is None:
            return None
        source = repo.get_source(owner_id, episode.source_id)
        graph_state = repo.get_graph_sync_state(owner_id, episode_id)
        if source is None or graph_state is None:
            return None
        return CapturedMemory(
            source=source,
            episode=episode,
            graph_sync=graph_state,
            outbox_job_id=uuid4(),
        )

    @contextmanager
    def _read_repo(self) -> Iterator[MemoryRepository]:
        """Open a short-lived read transaction that never commits mutations."""
        with self._uow_factory() as uow:
            yield self._repository_factory(uow.connection)

    @staticmethod
    def _require_owner(actor: ActorContext) -> None:
        """Observers may never mutate learner memory (Requirement 2.6)."""
        if not actor.is_owner:
            raise AuthorizationError("This action requires learner ownership.")
