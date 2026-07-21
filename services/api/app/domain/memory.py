"""Deliberate-memory domain model.

This module holds the pure value objects, lifecycle enumerations, typed errors,
and deterministic rules for deliberate memory capture, provenance, upload
quarantine, and the canonical-over-graph precedence policy. It contains no
persistence, framework, or provider dependencies so the rules stay
independently testable.

Design intent (Requirement 9 and 19):

* Memory is captured only on explicit save intent or a matching named auto-save
  rule with a recorded consent. Ordinary chat never becomes long-term memory.
* Every saved episode carries a ``Source_Record`` with origin, capture time,
  checksum, ownership, visibility, subject, and lifecycle state so a later
  graph-derived fact can cite its supporting evidence.
* Uploaded files are validated for size and type and screened for malware
  before ingestion; imported content is always treated as untrusted and its
  embedded instructions are never executed.
* ``Canonical_State`` is authoritative; Graph_Memory is retrieval augmentation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


# ---------------------------------------------------------------------------
# Lifecycle and classification enumerations
# ---------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Origin classification for a ``Source_Record``."""

    MANUAL_ENTRY = "manual_entry"
    PASTED_TEXT = "pasted_text"
    UPLOADED_FILE = "uploaded_file"
    CHAT_SUMMARY = "chat_summary"
    IMPORT = "import"


class EpisodeKind(StrEnum):
    """The deliberate kinds of content a learner may save."""

    NOTE = "note"
    ASSIGNMENT = "assignment"
    CORRECTION = "correction"
    GOAL = "goal"
    RESOURCE = "resource"
    CONVERSATION_SUMMARY = "conversation_summary"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


class EpisodeStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class Visibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"


class GraphSyncStatus(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    RETRACTED = "retracted"


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    REVOKED = "revoked"
    DECLINED = "declined"


class CaptureTrigger(StrEnum):
    """Why a capture is (or is not) written to long-term memory."""

    EXPLICIT_SAVE = "explicit_save"
    AUTO_SAVE_RULE = "auto_save_rule"


#: Canonical domains that always win over graph-derived augmentation
#: (Requirement 9.6 and 16.13).
CANONICAL_AUTHORITATIVE_KINDS: frozenset[str] = frozenset(
    {"assignments", "mastery", "curriculum", "schedules", "permissions", "lifecycle"}
)


# ---------------------------------------------------------------------------
# Typed domain errors
# ---------------------------------------------------------------------------


class MemoryError(RuntimeError):
    """Base class for typed, safe-to-surface memory errors."""

    code = "memory_error"
    retryable = False

    def safe_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class MemoryValidationError(MemoryError):
    """A field-level validation failure that must not change canonical state."""

    code = "validation_error"

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field

    def safe_payload(self) -> dict[str, Any]:
        payload = super().safe_payload()
        if self.field is not None:
            payload["field"] = self.field
        return payload


class UploadRejectedError(MemoryError):
    """An upload failed size/type validation or malware screening.

    The message and reasons never echo the untrusted file content so imported
    instructions cannot leak into a response (Requirements 19.12 and 19.13).
    """

    code = "upload_rejected"

    def __init__(self, message: str, reasons: list[str] | tuple[str, ...] | None = None) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons or ())

    def safe_payload(self) -> dict[str, Any]:
        payload = super().safe_payload()
        payload["reasons"] = list(self.reasons)
        return payload


class ConsentRequiredError(MemoryError):
    """A capture was attempted without a granted consent record."""

    code = "consent_required"

    def __init__(self, message: str = "A granted consent is required for this capture.") -> None:
        super().__init__(message)


class AutoSaveRuleError(MemoryError):
    """A named auto-save rule is missing, disabled, or does not match."""

    code = "auto_save_rule_error"


# ---------------------------------------------------------------------------
# Upload validation and malware-screening port
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadLimits:
    """Configured upload size and content-type limits (Requirement 19.11)."""

    max_bytes: int = 10 * 1024 * 1024
    allowed_content_types: frozenset[str] = frozenset(
        {
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/pdf",
            "image/png",
            "image/jpeg",
        }
    )


@dataclass(frozen=True)
class FileUpload:
    """An untrusted uploaded file offered for ingestion."""

    filename: str
    content_type: str
    content: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class ScanResult:
    """Outcome of a malware/content screen for an uploaded file."""

    clean: bool
    detail: str | None = None


class FileScanner(Protocol):
    """Port for size/type-aware malware screening of untrusted uploads.

    Implementations live in the adapters layer. The service always screens an
    upload before ingestion and quarantines anything that is not clean.
    """

    def scan(self, upload: FileUpload) -> ScanResult: ...


# ---------------------------------------------------------------------------
# Provenance and capture decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Source, evidence, date, rule, confidence, and ownership for saved content."""

    source_kind: str
    owner_user_id: UUID
    captured_at: datetime
    checksum: str
    trigger: str
    origin: str | None = None
    subject_id: UUID | None = None
    rule_name: str | None = None
    confidence: float | None = None
    visibility: str = Visibility.PRIVATE.value
    untrusted: bool = False
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable representation for the ``sources`` metadata column."""
        return {
            "source_kind": self.source_kind,
            "owner_user_id": str(self.owner_user_id),
            "captured_at": self.captured_at.astimezone(timezone.utc).isoformat(),
            "checksum": self.checksum,
            "trigger": self.trigger,
            "origin": self.origin,
            "subject_id": str(self.subject_id) if self.subject_id is not None else None,
            "rule_name": self.rule_name,
            "confidence": self.confidence,
            "visibility": self.visibility,
            "untrusted": self.untrusted,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class CaptureDecision:
    """Whether a candidate turn is written to long-term memory, and why."""

    persist: bool
    trigger: CaptureTrigger | None
    reason: str


# ---------------------------------------------------------------------------
# Persisted memory entities (framework-free projections)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """A ``Source_Record`` identifying the origin of saved or generated content."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID | None
    kind: str
    title: str | None
    uri: str | None
    content_checksum: str
    provenance: dict[str, Any]
    status: SourceStatus
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class MemoryEpisode:
    """A deliberately saved note, assignment, correction, goal, or resource."""

    id: UUID
    owner_user_id: UUID
    subject_id: UUID | None
    source_id: UUID
    kind: str
    content: str
    visibility: str
    user_confidence: float | None
    status: EpisodeStatus


@dataclass(frozen=True)
class SourceChunk:
    """A live, owner-scoped source chunk returned for similarity ranking.

    The repository applies the authenticated owner, permitted-subject, date, and
    live-lifecycle filters *before* returning candidates, so similarity ranking
    only ever sees authorized rows (Requirements 9.7, 10.3).
    """

    id: UUID
    owner_user_id: UUID
    subject_id: UUID | None
    source_id: UUID
    episode_id: UUID | None
    position: int
    content: str
    embedding: tuple[float, ...] | None
    metadata: dict[str, Any]
    user_confidence: float | None
    created_at: datetime


@dataclass(frozen=True)
class GraphSyncState:
    """The graph-ingestion synchronization state for an accepted episode."""

    id: UUID
    owner_user_id: UUID
    episode_id: UUID
    status: GraphSyncStatus
    graph_group: str
    attempt_count: int
    last_error_code: str | None = None
    synced_at: datetime | None = None


@dataclass(frozen=True)
class Consent:
    """A recorded consent supporting a capture or auto-save rule."""

    id: UUID
    owner_user_id: UUID
    kind: str
    status: ConsentStatus
    policy_version: str
    granted_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_granted(self) -> bool:
        return self.status is ConsentStatus.GRANTED


@dataclass(frozen=True)
class AutoSaveRule:
    """A learner-configured named rule that captures covered content."""

    id: UUID
    owner_user_id: UUID
    name: str
    source_kind: str
    consent_id: UUID
    enabled: bool
    rule_json: dict[str, Any]


@dataclass(frozen=True)
class CapturedMemory:
    """The result of an accepted capture transaction."""

    source: Source
    episode: MemoryEpisode
    graph_sync: GraphSyncState
    outbox_job_id: UUID


@dataclass(frozen=True)
class ChatCaptureResult:
    """The outcome of offering a chat turn to memory."""

    decision: CaptureDecision
    captured: CapturedMemory | None = None


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def normalize_content(content: str | None) -> str:
    """Strip and require non-empty save content (Requirement 9.11).

    Empty or whitespace-only content raises a validation error so no
    ``Source_Record`` or ``Memory_Episode`` is created.
    """
    clean = (content or "").strip()
    if not clean:
        raise MemoryValidationError("Save content must not be empty.", field="content")
    return clean


def compute_checksum(material: str | bytes) -> str:
    """Deterministic SHA-256 checksum for source-content integrity."""
    data = material.encode("utf-8") if isinstance(material, str) else material
    return hashlib.sha256(data).hexdigest()


def validate_confidence(confidence: float | None) -> float | None:
    """User-provided confidence must be within the inclusive range 0..1."""
    if confidence is None:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MemoryValidationError("Confidence must be a number between 0 and 1.", field="user_confidence")
    if not 0.0 <= float(confidence) <= 1.0:
        raise MemoryValidationError("Confidence must be between 0 and 1.", field="user_confidence")
    return float(confidence)


def coerce_episode_kind(kind: str | EpisodeKind) -> EpisodeKind:
    """Validate the requested episode kind against the supported vocabulary."""
    if isinstance(kind, EpisodeKind):
        return kind
    try:
        return EpisodeKind(str(kind))
    except ValueError as error:
        raise MemoryValidationError("Unsupported memory kind.", field="kind") from error


def validate_upload(upload: FileUpload, limits: UploadLimits) -> None:
    """Enforce configured size and type limits before ingestion (Requirement 19.11).

    A rejection never includes the untrusted file content.
    """
    reasons: list[str] = []
    if not (upload.filename or "").strip():
        reasons.append("missing_filename")
    if upload.size_bytes == 0:
        reasons.append("empty_file")
    if upload.size_bytes > limits.max_bytes:
        reasons.append("size_limit_exceeded")
    content_type = (upload.content_type or "").strip().lower()
    if content_type not in limits.allowed_content_types:
        reasons.append("unsupported_content_type")
    if reasons:
        raise UploadRejectedError("Uploaded file failed validation.", reasons=reasons)


def decide_capture(*, explicit_save: bool, matching_rule: bool) -> CaptureDecision:
    """Decide whether a candidate turn becomes long-term memory (Requirement 9.3).

    Absent explicit save intent and a matching auto-save rule, chat content is
    deliberately kept out of long-term ``Memory_Episodes``.
    """
    if explicit_save:
        return CaptureDecision(True, CaptureTrigger.EXPLICIT_SAVE, "Explicit save intent.")
    if matching_rule:
        return CaptureDecision(True, CaptureTrigger.AUTO_SAVE_RULE, "Matching auto-save rule and consent.")
    return CaptureDecision(
        False,
        None,
        "No explicit save intent or matching auto-save rule; chat stays out of long-term memory.",
    )


def graph_group(owner_user_id: UUID, subject_id: UUID | None) -> str:
    """Derive the Graphiti group from the authenticated owner scope (Requirement 9.4)."""
    if subject_id is None:
        return f"user:{owner_user_id}"
    return f"user:{owner_user_id}:subject:{subject_id}"


def canonical_is_authoritative(resource_kind: str) -> bool:
    """True when Canonical_State overrides graph augmentation (Requirement 9.6)."""
    return resource_kind in CANONICAL_AUTHORITATIVE_KINDS


def build_provenance(
    *,
    source_kind: SourceKind | str,
    owner_user_id: UUID,
    captured_at: datetime,
    checksum: str,
    trigger: CaptureTrigger,
    origin: str | None = None,
    subject_id: UUID | None = None,
    rule_name: str | None = None,
    confidence: float | None = None,
    visibility: Visibility | str = Visibility.PRIVATE,
    untrusted: bool = False,
    evidence: tuple[str, ...] = (),
) -> Provenance:
    """Assemble a provenance value object for a capture."""
    return Provenance(
        source_kind=str(source_kind),
        owner_user_id=owner_user_id,
        captured_at=captured_at,
        checksum=checksum,
        trigger=str(trigger),
        origin=origin,
        subject_id=subject_id,
        rule_name=rule_name,
        confidence=confidence,
        visibility=str(visibility),
        untrusted=untrusted,
        evidence=evidence,
    )
