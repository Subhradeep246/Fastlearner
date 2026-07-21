"""Vendor-neutral AI provider contracts and ports.

This module defines the provider-independent language, streaming, structured
output, embedding, and optional speech contracts that deterministic domain and
application services depend on. It contains no provider SDK, framework, or
network dependency so the learning domain never binds to a specific vendor
(Requirements 11.1, 11.4, 11.7, and 11.10).

Design intent:

* ``AIProvider`` is the single vendor-neutral seam. Adapters (for example the
  configured OpenAI adapter) implement it and translate provider-specific
  requests and responses into the shared contracts below. No provider SDK type
  ever crosses this boundary.
* Every provider failure is a typed :class:`ProviderError`. Because these
  contracts return values or raise typed errors and never touch persistence,
  an unavailable provider or invalid structured output can never mutate
  ``Canonical_State`` (Requirements 11.5 and 11.6).
* Language-model output is always returned as a normalized draft/result value;
  it is never treated as a source of truth (Requirement 11.10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncIterator, Mapping, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


# ---------------------------------------------------------------------------
# Message and generation contracts
# ---------------------------------------------------------------------------


class MessageRole(StrEnum):
    """Vendor-neutral conversational roles."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Why a generation stopped, normalized across providers."""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALL = "tool_call"
    ERROR = "error"


@dataclass(frozen=True)
class Message:
    """A single conversational message supplied to or returned by a provider."""

    role: MessageRole
    content: str


@dataclass(frozen=True)
class Usage:
    """Normalized token accounting for a provider call (Requirement 11.4)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def usage_from(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None = None,
) -> Usage:
    """Build :class:`Usage`, defaulting the total to prompt + completion."""

    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    total = int(total_tokens) if total_tokens is not None else prompt + completion
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


@dataclass(frozen=True)
class GenerationRequest:
    """A vendor-neutral request for a single text generation."""

    messages: tuple[Message, ...]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    stop: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationResult:
    """Normalized output of a single text generation (Requirement 11.4)."""

    text: str
    usage: Usage
    finish_reason: FinishReason
    model: str


# ---------------------------------------------------------------------------
# Streaming events (a normalized discriminated union)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTextDelta:
    """An incremental chunk of generated text."""

    text: str
    type: str = "text_delta"


@dataclass(frozen=True)
class ProviderUsageUpdate:
    """An interim or final usage report emitted during streaming."""

    usage: Usage
    type: str = "usage"


@dataclass(frozen=True)
class ProviderCompleted:
    """The terminal streaming event carrying the finish reason and usage."""

    finish_reason: FinishReason
    usage: Usage
    type: str = "completed"


#: The normalized streaming event union. Adapters translate provider-specific
#: chunks into exactly these shapes.
ProviderEvent = ProviderTextDelta | ProviderUsageUpdate | ProviderCompleted


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class StructuredValidationError(Exception):
    """Raised by a :class:`StructuredValidator` when data violates its schema.

    Adapters translate this into a typed :class:`ProviderInvalidOutputError` so
    invalid structured output never becomes a domain draft (Requirement 11.6).
    """


class StructuredValidator(Protocol[T_co]):
    """Validates raw provider JSON output into a typed domain value.

    Implementations (for example a thin wrapper over a Pydantic model) must
    raise :class:`StructuredValidationError` when the data does not conform.
    """

    def validate(self, data: Mapping[str, Any]) -> T_co: ...


@dataclass(frozen=True)
class StructuredOutputRequest[T]:
    """A request for schema-validated structured output.

    ``validator`` converts the provider's JSON object into a typed value and
    raises :class:`StructuredValidationError` on any contract violation.
    ``json_schema`` and ``schema_name`` let adapters request native structured
    output modes where available (Requirement 11.6).
    """

    messages: tuple[Message, ...]
    validator: StructuredValidator[T]
    schema_name: str = "structured_output"
    json_schema: Mapping[str, Any] | None = None
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Optional speech contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptionRequest:
    """A speech-to-text request over raw local audio bytes."""

    audio: bytes
    content_type: str = "audio/wav"
    language: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    """Normalized speech-to-text output."""

    text: str
    model: str
    usage: Usage | None = None


@dataclass(frozen=True)
class SpeechSynthesisRequest:
    """A text-to-speech request."""

    text: str
    voice: str | None = None
    model: str | None = None
    audio_format: str = "mp3"


@dataclass(frozen=True)
class SpeechSynthesisResult:
    """Normalized text-to-speech output."""

    audio: bytes
    content_type: str
    model: str


# ---------------------------------------------------------------------------
# Typed provider errors (Requirements 11.5 and 11.6)
# ---------------------------------------------------------------------------


class ProviderErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    INVALID_OUTPUT = "invalid_output"
    SAFETY_REJECTED = "safety_rejected"
    CONFIGURATION = "configuration"


class ProviderError(RuntimeError):
    """Base class for typed, safe-to-surface AI provider errors.

    These errors are the only failures an adapter raises. They never carry
    provider SDK objects and, because adapters do not persist anything, they
    cannot change ``Canonical_State``.
    """

    code: str = ProviderErrorCode.UNAVAILABLE.value
    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        if retryable is not None:
            self.retryable = retryable

    def safe_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.provider is not None:
            payload["provider"] = self.provider
        return payload


class ProviderUnavailableError(ProviderError):
    """The configured provider could not be reached or is disabled."""

    code = ProviderErrorCode.UNAVAILABLE.value
    retryable = True


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured deadline."""

    code = ProviderErrorCode.TIMEOUT.value
    retryable = True


class ProviderRateLimitedError(ProviderError):
    """The provider rejected the request because a rate limit was exceeded."""

    code = ProviderErrorCode.RATE_LIMITED.value
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_seconds = retry_after_seconds

    def safe_payload(self) -> dict[str, Any]:
        payload = super().safe_payload()
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return payload


class ProviderInvalidOutputError(ProviderError):
    """Structured output failed contract validation (Requirement 11.6)."""

    code = ProviderErrorCode.INVALID_OUTPUT.value
    retryable = False


class ProviderSafetyError(ProviderError):
    """The provider refused or filtered the request for safety reasons."""

    code = ProviderErrorCode.SAFETY_REJECTED.value
    retryable = False


class ProviderConfigurationError(ProviderError):
    """A required provider setting (for example the model) is missing."""

    code = ProviderErrorCode.CONFIGURATION.value
    retryable = False


# ---------------------------------------------------------------------------
# Vendor-neutral ports
# ---------------------------------------------------------------------------


@runtime_checkable
class AIProvider(Protocol):
    """The vendor-neutral language-capability port (Requirement 11.1).

    Deterministic domain services depend only on this contract, so replacing
    the provider requires no change to mastery, planning, authorization, or
    memory rules (Requirement 11.7).
    """

    @property
    def name(self) -> str: ...

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...

    def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderEvent]: ...

    async def structured(self, request: StructuredOutputRequest[T]) -> T: ...

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...


@runtime_checkable
class SpeechProvider(Protocol):
    """Optional speech-to-text and text-to-speech capabilities (Requirement 11.1)."""

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...

    async def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult: ...


# ---------------------------------------------------------------------------
# Small construction helpers
# ---------------------------------------------------------------------------


def system_message(content: str) -> Message:
    return Message(role=MessageRole.SYSTEM, content=content)


def user_message(content: str) -> Message:
    return Message(role=MessageRole.USER, content=content)


def assistant_message(content: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=content)
