"""Unit and integration tests for deliberate memory capture and provenance.

Covers Requirements 9.1, 9.2, 9.3, 9.5, 9.6, 9.10, 9.11, 19.11, 19.12, and
19.13: explicit-save capture with provenance and a pending graph-sync job,
named-rule capture gated by consent, keeping ordinary chat out of long-term
memory, canonical precedence over graph augmentation, failed graph-sync state,
empty-content validation, and upload size/type/malware boundaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.adapters.files import SignatureFileScanner
from app.domain.identity import ActorContext, AuthorizationError, LEARNER_OWNER_SCOPES, Role
from app.domain.memory import (
    CaptureTrigger,
    ConsentRequiredError,
    EpisodeKind,
    FileUpload,
    GraphSyncStatus,
    MemoryValidationError,
    SourceKind,
    SourceStatus,
    UploadLimits,
    UploadRejectedError,
    canonical_is_authoritative,
    compute_checksum,
    decide_capture,
    graph_group,
    normalize_content,
    validate_upload,
)
from app.persistence.models import (
    audit_records,
    graph_sync_state,
    memory_episodes,
    metadata,
    outbox_jobs,
    sources,
)
from app.persistence.seeds import LOCAL_LEARNER_ID, LOCAL_PARENT_ID, seed_local_personas
from app.repositories import unit_of_work
from app.repositories.memory import SqlMemoryRepository
from app.services.memory import MemoryService

_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _clock() -> FakeClock:
    return FakeClock(datetime(2025, 1, 1, tzinfo=timezone.utc))


def _seeded_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        seed_local_personas(connection)
    return engine


def _learner() -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_LEARNER_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.LEARNER,
        scopes=LEARNER_OWNER_SCOPES,
    )


def _observer() -> ActorContext:
    return ActorContext(
        actor_id=LOCAL_PARENT_ID,
        owner_id=LOCAL_LEARNER_ID,
        role=Role.PARENT,
        scopes=frozenset({"memory:read"}),
    )


def _service(engine, clock, *, scanner=None, limits=None) -> MemoryService:
    return MemoryService(
        lambda: unit_of_work(engine, clock),
        scanner=scanner or SignatureFileScanner(),
        repository_factory=SqlMemoryRepository,
        clock=clock,
        limits=limits,
    )


def _count(engine, table) -> int:
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def test_normalize_content_rejects_empty() -> None:
    assert normalize_content("  hello  ") == "hello"
    for empty in ("", "   ", None):
        with pytest.raises(MemoryValidationError):
            normalize_content(empty)  # type: ignore[arg-type]


def test_graph_group_uses_owner_scope() -> None:
    owner = uuid4()
    subject = uuid4()
    assert graph_group(owner, None) == f"user:{owner}"
    assert graph_group(owner, subject) == f"user:{owner}:subject:{subject}"


def test_canonical_precedence_over_graph_augmentation() -> None:
    for kind in ("assignments", "mastery", "curriculum", "schedules", "permissions", "lifecycle"):
        assert canonical_is_authoritative(kind) is True
    assert canonical_is_authoritative("graph_fact") is False


def test_decide_capture_keeps_chat_out_by_default() -> None:
    assert decide_capture(explicit_save=False, matching_rule=False).persist is False
    assert decide_capture(explicit_save=True, matching_rule=False).trigger is CaptureTrigger.EXPLICIT_SAVE
    assert decide_capture(explicit_save=False, matching_rule=True).trigger is CaptureTrigger.AUTO_SAVE_RULE


def test_validate_upload_size_and_type() -> None:
    limits = UploadLimits(max_bytes=10, allowed_content_types=frozenset({"text/plain"}))
    validate_upload(FileUpload("a.txt", "text/plain", b"hello"), limits)
    with pytest.raises(UploadRejectedError) as too_big:
        validate_upload(FileUpload("a.txt", "text/plain", b"x" * 20), limits)
    assert "size_limit_exceeded" in too_big.value.reasons
    with pytest.raises(UploadRejectedError) as bad_type:
        validate_upload(FileUpload("a.bin", "application/octet-stream", b"x"), limits)
    assert "unsupported_content_type" in bad_type.value.reasons


# ---------------------------------------------------------------------------
# Explicit save (Requirements 9.1, 9.5, 9.11)
# ---------------------------------------------------------------------------


def test_save_context_persists_episode_source_graph_and_outbox() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)

    captured = service.save_context(
        _learner(),
        content="Remember: fractions add over a common denominator.",
        kind=EpisodeKind.NOTE,
        idempotency_key="save-1",
    )

    assert captured.episode.content.startswith("Remember")
    assert captured.source.status is SourceStatus.ACTIVE
    assert captured.source.provenance["trigger"] == CaptureTrigger.EXPLICIT_SAVE.value
    assert captured.source.content_checksum == compute_checksum(
        "Remember: fractions add over a common denominator."
    )
    assert captured.graph_sync.status is GraphSyncStatus.PENDING
    assert captured.graph_sync.graph_group == f"user:{LOCAL_LEARNER_ID}"

    assert _count(engine, memory_episodes) == 1
    assert _count(engine, sources) == 1
    with engine.connect() as connection:
        job = connection.execute(
            select(outbox_jobs).where(outbox_jobs.c.kind == "graph_ingestion")
        ).mappings().one()
        assert job["payload"]["episode_id"] == str(captured.episode.id)
        assert "content" not in job["payload"]  # payload carries IDs, not content
        audit = connection.execute(
            select(audit_records).where(audit_records.c.action == "memory.save")
        ).mappings().one()
        assert audit["resource_id"] == captured.episode.id


def test_save_context_empty_content_writes_nothing() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    with pytest.raises(MemoryValidationError):
        service.save_context(_learner(), content="   ", kind=EpisodeKind.NOTE)
    assert _count(engine, memory_episodes) == 0
    assert _count(engine, sources) == 0


def test_save_context_idempotent_replay_returns_same_episode() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    first = service.save_context(
        _learner(), content="A durable note.", kind=EpisodeKind.NOTE, idempotency_key="dup-1"
    )
    second = service.save_context(
        _learner(), content="A durable note.", kind=EpisodeKind.NOTE, idempotency_key="dup-1"
    )
    assert first.episode.id == second.episode.id
    assert _count(engine, memory_episodes) == 1


def test_observer_cannot_save_memory() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    with pytest.raises(AuthorizationError):
        service.save_context(_observer(), content="not allowed", kind=EpisodeKind.NOTE)
    assert _count(engine, memory_episodes) == 0


# ---------------------------------------------------------------------------
# Chat boundary (Requirement 9.3)
# ---------------------------------------------------------------------------


def test_ordinary_chat_is_not_saved() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    result = service.capture_chat_turn(_learner(), content="just chatting about my day")
    assert result.decision.persist is False
    assert result.captured is None
    assert _count(engine, memory_episodes) == 0


def test_explicit_chat_save_is_persisted() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    result = service.capture_chat_turn(
        _learner(), content="Save this: I struggle with unlike denominators.", explicit_save=True
    )
    assert result.decision.persist is True
    assert result.captured is not None
    assert _count(engine, memory_episodes) == 1


# ---------------------------------------------------------------------------
# Auto-save rule capture (Requirement 9.2)
# ---------------------------------------------------------------------------


def _grant_rule(service: MemoryService, learner: ActorContext, *, source_kind=SourceKind.IMPORT):
    consent = service.record_consent(learner, kind="auto_save", policy_version="v1")
    rule = service.create_auto_save_rule(
        learner,
        name="imports",
        source_kind=source_kind,
        consent_id=consent.id,
        rule_json={"match": "lms"},
    )
    return consent, rule


def test_capture_by_rule_records_named_rule_provenance() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    learner = _learner()
    _grant_rule(service, learner)

    captured = service.capture_by_rule(
        learner, rule_name="imports", content="Imported syllabus summary.", source_kind=SourceKind.IMPORT
    )
    assert captured.source.provenance["trigger"] == CaptureTrigger.AUTO_SAVE_RULE.value
    assert captured.source.provenance["rule_name"] == "imports"
    assert _count(engine, memory_episodes) == 1


def test_capture_by_rule_rejects_uncovered_content() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    learner = _learner()
    _grant_rule(service, learner, source_kind=SourceKind.IMPORT)
    with pytest.raises(MemoryValidationError):
        service.capture_by_rule(
            learner, rule_name="imports", content="x", source_kind=SourceKind.CHAT_SUMMARY
        )
    assert _count(engine, memory_episodes) == 0


def test_capture_by_rule_requires_granted_consent() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    learner = _learner()
    consent, _rule = _grant_rule(service, learner)
    service.revoke_consent(learner, consent.id)
    with pytest.raises(ConsentRequiredError):
        service.capture_by_rule(learner, rule_name="imports", content="blocked", source_kind=SourceKind.IMPORT)
    assert _count(engine, memory_episodes) == 0


def test_missing_rule_is_rejected() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    from app.domain.memory import AutoSaveRuleError

    with pytest.raises(AutoSaveRuleError):
        service.capture_by_rule(_learner(), rule_name="nope", content="data")


# ---------------------------------------------------------------------------
# Uploads and untrusted content (Requirements 19.11, 19.12, 19.13)
# ---------------------------------------------------------------------------


def test_clean_upload_is_captured_as_untrusted() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    upload = FileUpload("notes.txt", "text/plain", b"clean study notes")
    captured = service.save_context(
        _learner(), content="Uploaded my notes.", kind=EpisodeKind.RESOURCE, upload=upload
    )
    assert captured.source.kind == SourceKind.UPLOADED_FILE.value
    assert captured.source.provenance["untrusted"] is True
    assert captured.source.content_checksum == compute_checksum(b"clean study notes")


def test_oversized_upload_is_rejected_without_ingestion() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock, limits=UploadLimits(max_bytes=8, allowed_content_types=frozenset({"text/plain"})))
    upload = FileUpload("big.txt", "text/plain", b"way too many bytes")
    with pytest.raises(UploadRejectedError) as rejected:
        service.save_context(_learner(), content="doc", kind=EpisodeKind.RESOURCE, upload=upload)
    assert "size_limit_exceeded" in rejected.value.reasons
    assert _count(engine, memory_episodes) == 0
    assert _count(engine, sources) == 0


def test_malicious_upload_is_quarantined_and_rejected() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    upload = FileUpload("payload.txt", "text/plain", _EICAR)
    with pytest.raises(UploadRejectedError) as rejected:
        service.save_context(_learner(), content="doc", kind=EpisodeKind.RESOURCE, upload=upload)
    assert "eicar_test_signature" in rejected.value.reasons
    # No episode is created, but a quarantined source records the rejection.
    assert _count(engine, memory_episodes) == 0
    with engine.connect() as connection:
        row = connection.execute(select(sources)).mappings().one()
        assert row["status"] == SourceStatus.QUARANTINED.value
        assert row["provenance"]["untrusted"] is True
        audit = connection.execute(
            select(audit_records).where(audit_records.c.action == "memory.upload_quarantined")
        ).mappings().one()
        assert audit["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# Failed graph sync state (Requirement 9.10)
# ---------------------------------------------------------------------------


def test_graph_sync_failure_retains_episode_and_marks_failed() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    learner = _learner()
    captured = service.save_context(learner, content="A note to sync.", kind=EpisodeKind.NOTE)

    state = service.record_graph_sync_failure(
        learner, captured.episode.id, error_code="graph_unavailable"
    )
    assert state.status is GraphSyncStatus.FAILED
    assert state.attempt_count == 1
    assert state.last_error_code == "graph_unavailable"

    # The accepted local episode is untouched and remains active.
    with engine.connect() as connection:
        episode = connection.execute(
            select(memory_episodes).where(memory_episodes.c.id == captured.episode.id)
        ).mappings().one()
        assert episode["status"] == "active"
        sync = connection.execute(
            select(graph_sync_state).where(graph_sync_state.c.episode_id == captured.episode.id)
        ).mappings().one()
        assert sync["status"] == "failed"


def test_graph_sync_failure_unknown_episode_is_validation_error() -> None:
    engine = _seeded_engine()
    clock = _clock()
    service = _service(engine, clock)
    with pytest.raises(MemoryValidationError):
        service.record_graph_sync_failure(_learner(), uuid4(), error_code="x")
