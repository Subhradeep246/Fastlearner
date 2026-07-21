"""AI provider adapters implementing the vendor-neutral :mod:`app.domain.ai` port.

The configured OpenAI adapter translates neutral requests into OpenAI REST
payloads through a small injectable transport, and translates the transport's
plain-dictionary responses back into the shared contracts. No OpenAI SDK type
ever crosses the :class:`~app.domain.ai.AIProvider` boundary (Requirements 11.1,
11.2, and 11.4).

Provider selection is server-side: :func:`create_ai_provider` reads
``AI_PROVIDER`` and the model/embedding settings and never exposes them to a
frontend bundle (Requirement 11.3). A ``disabled`` selection yields a provider
that returns typed unavailable errors so AI-dependent actions degrade safely
while deterministic functions keep working (Requirement 11.8).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence, TypeVar, cast

from app.domain.ai import (
    AIProvider,
    FinishReason,
    GenerationRequest,
    GenerationResult,
    ProviderCompleted,
    ProviderConfigurationError,
    ProviderError,
    ProviderEvent,
    ProviderInvalidOutputError,
    ProviderRateLimitedError,
    ProviderSafetyError,
    ProviderTextDelta,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SpeechProvider,
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    StructuredOutputRequest,
    StructuredValidationError,
    TranscriptionRequest,
    TranscriptionResult,
    Usage,
    usage_from,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

T = TypeVar("T")

_PROVIDER_NAME = "openai"

#: Maps OpenAI finish reasons to the neutral :class:`FinishReason` vocabulary.
_FINISH_REASONS: Mapping[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_CALL,
    "function_call": FinishReason.TOOL_CALL,
}


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------


class OpenAITransportError(Exception):
    """A transport-level failure categorized for neutral error mapping.

    ``category`` is one of ``unavailable``, ``timeout``, ``rate_limited``, or
    ``safety``. The transport never raises provider SDK exceptions across this
    boundary; it raises this typed error instead.
    """

    def __init__(
        self,
        category: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after_seconds = retry_after_seconds


class OpenAITransport:
    """Low-level transport contract returning plain dictionaries.

    Implementations issue the actual OpenAI REST calls. Tests inject a fake
    transport so no real network request is ever made.
    """

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def chat_stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    async def embeddings(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def transcribe(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def synthesize(self, payload: Mapping[str, Any]) -> bytes:  # pragma: no cover
        raise NotImplementedError


def _map_transport_error(error: OpenAITransportError) -> ProviderError:
    """Translate a transport error category into a typed provider error."""

    message = str(error)
    if error.category == "timeout":
        return ProviderTimeoutError(message, provider=_PROVIDER_NAME)
    if error.category == "rate_limited":
        return ProviderRateLimitedError(
            message, provider=_PROVIDER_NAME, retry_after_seconds=error.retry_after_seconds
        )
    if error.category == "safety":
        return ProviderSafetyError(message, provider=_PROVIDER_NAME)
    return ProviderUnavailableError(message, provider=_PROVIDER_NAME)


# ---------------------------------------------------------------------------
# OpenAI provider adapter
# ---------------------------------------------------------------------------


class OpenAIProvider(AIProvider, SpeechProvider):
    """The configured OpenAI adapter (Requirement 11.2)."""

    def __init__(
        self,
        transport: OpenAITransport,
        *,
        model: str | None = None,
        embedding_model: str | None = None,
        transcription_model: str | None = None,
        speech_model: str | None = None,
        default_voice: str = "alloy",
    ) -> None:
        self._transport = transport
        self._model = model
        self._embedding_model = embedding_model
        self._transcription_model = transcription_model
        self._speech_model = speech_model
        self._default_voice = default_voice

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    # -- request building ---------------------------------------------------

    def _resolve_model(self, requested: str | None, configured: str | None, setting: str) -> str:
        model = requested or configured
        if not model:
            raise ProviderConfigurationError(
                f"No {setting} is configured for the OpenAI provider.", provider=_PROVIDER_NAME
            )
        return model

    def _chat_payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._resolve_model(request.model, self._model, "AI_MODEL"),
            "messages": [{"role": str(message.role), "content": message.content} for message in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.stop:
            payload["stop"] = list(request.stop)
        return payload

    # -- generation ---------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = self._chat_payload(request)
        try:
            response = await self._transport.chat(payload)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error
        return self._parse_generation(response, payload["model"])

    def _parse_generation(self, response: Mapping[str, Any], model: str) -> GenerationResult:
        choice = _first_choice(response)
        finish_reason = _finish_reason(choice.get("finish_reason"))
        if finish_reason is FinishReason.CONTENT_FILTER:
            raise ProviderSafetyError(
                "The provider filtered the response for safety.", provider=_PROVIDER_NAME
            )
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            raise ProviderInvalidOutputError(
                "The provider returned no text content.", provider=_PROVIDER_NAME
            )
        return GenerationResult(
            text=text,
            usage=_usage(response.get("usage")),
            finish_reason=finish_reason,
            model=str(response.get("model") or model),
        )

    # -- streaming ----------------------------------------------------------

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderEvent]:
        payload = {**self._chat_payload(request), "stream": True}
        aggregated = Usage()
        completed = False
        try:
            async for chunk in self._transport.chat_stream(payload):
                choice = _first_choice(chunk, default_empty=True)
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield ProviderTextDelta(text=content)
                usage_payload = chunk.get("usage")
                if usage_payload:
                    aggregated = _usage(usage_payload)
                raw_reason = choice.get("finish_reason")
                if raw_reason:
                    reason = _finish_reason(raw_reason)
                    if reason is FinishReason.CONTENT_FILTER:
                        raise ProviderSafetyError(
                            "The provider filtered the response for safety.", provider=_PROVIDER_NAME
                        )
                    completed = True
                    yield ProviderCompleted(finish_reason=reason, usage=aggregated)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error
        if not completed:
            yield ProviderCompleted(finish_reason=FinishReason.STOP, usage=aggregated)

    # -- structured output --------------------------------------------------

    async def structured(self, request: StructuredOutputRequest[T]) -> T:
        payload: dict[str, Any] = {
            "model": self._resolve_model(request.model, self._model, "AI_MODEL"),
            "messages": [{"role": str(message.role), "content": message.content} for message in request.messages],
            "response_format": _response_format(request),
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        try:
            response = await self._transport.chat(payload)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error

        choice = _first_choice(response)
        if _finish_reason(choice.get("finish_reason")) is FinishReason.CONTENT_FILTER:
            raise ProviderSafetyError(
                "The provider filtered the structured response for safety.", provider=_PROVIDER_NAME
            )
        raw = (choice.get("message") or {}).get("content")
        if not isinstance(raw, str):
            raise ProviderInvalidOutputError(
                "The provider returned no structured content.", provider=_PROVIDER_NAME
            )
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise ProviderInvalidOutputError(
                "The provider returned malformed structured output.", provider=_PROVIDER_NAME
            ) from error
        if not isinstance(data, Mapping):
            raise ProviderInvalidOutputError(
                "Structured output must be a JSON object.", provider=_PROVIDER_NAME
            )
        try:
            return request.validator.validate(data)
        except StructuredValidationError as error:
            raise ProviderInvalidOutputError(str(error) or "Structured output failed validation.", provider=_PROVIDER_NAME) from error

    # -- embeddings ---------------------------------------------------------

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self._resolve_model(model, self._embedding_model, "EMBEDDING_MODEL"),
            "input": list(texts),
        }
        try:
            response = await self._transport.embeddings(payload)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error
        return _parse_embeddings(response, expected=len(texts))

    # -- speech (optional) --------------------------------------------------

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        model = self._resolve_model(request.model, self._transcription_model, "AI_TRANSCRIPTION_MODEL")
        payload: dict[str, Any] = {
            "model": model,
            "audio": request.audio,
            "content_type": request.content_type,
        }
        if request.language:
            payload["language"] = request.language
        try:
            response = await self._transport.transcribe(payload)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error
        text = response.get("text")
        if not isinstance(text, str):
            raise ProviderInvalidOutputError(
                "The provider returned no transcription text.", provider=_PROVIDER_NAME
            )
        usage_payload = response.get("usage")
        return TranscriptionResult(
            text=text,
            model=str(response.get("model") or model),
            usage=_usage(usage_payload) if usage_payload else None,
        )

    async def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        model = self._resolve_model(request.model, self._speech_model, "AI_SPEECH_MODEL")
        payload = {
            "model": model,
            "input": request.text,
            "voice": request.voice or self._default_voice,
            "format": request.audio_format,
        }
        try:
            audio = await self._transport.synthesize(payload)
        except OpenAITransportError as error:
            raise _map_transport_error(error) from error
        if not isinstance(audio, (bytes, bytearray)):
            raise ProviderInvalidOutputError(
                "The provider returned no synthesized audio.", provider=_PROVIDER_NAME
            )
        return SpeechSynthesisResult(
            audio=bytes(audio),
            content_type=f"audio/{request.audio_format}",
            model=model,
        )


# ---------------------------------------------------------------------------
# Disabled provider (Requirement 11.8)
# ---------------------------------------------------------------------------


class DisabledAIProvider(AIProvider, SpeechProvider):
    """A provider that reports every AI-dependent action as unavailable.

    Selected when ``AI_PROVIDER=disabled`` so the system keeps deterministic
    functions while clearly surfacing that AI capabilities are offline.
    """

    _MESSAGE = "The AI provider is disabled by server configuration."

    @property
    def name(self) -> str:
        return "disabled"

    def _unavailable(self) -> ProviderUnavailableError:
        return ProviderUnavailableError(self._MESSAGE, provider=self.name)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise self._unavailable()

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderEvent]:
        raise self._unavailable()
        yield ProviderCompleted(finish_reason=FinishReason.ERROR, usage=Usage())  # pragma: no cover

    async def structured(self, request: StructuredOutputRequest[T]) -> T:
        raise self._unavailable()

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        raise self._unavailable()

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise self._unavailable()

    async def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        raise self._unavailable()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _first_choice(response: Mapping[str, Any], *, default_empty: bool = False) -> Mapping[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, Sequence) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            return first
    if default_empty:
        return {}
    raise ProviderInvalidOutputError(
        "The provider response contained no choices.", provider=_PROVIDER_NAME
    )


def _finish_reason(raw: Any) -> FinishReason:
    if not raw:
        return FinishReason.STOP
    return _FINISH_REASONS.get(str(raw), FinishReason.STOP)


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    return usage_from(
        raw.get("prompt_tokens"),
        raw.get("completion_tokens"),
        raw.get("total_tokens"),
    )


def _response_format(request: StructuredOutputRequest[Any]) -> dict[str, Any]:
    if request.json_schema is not None:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name,
                "schema": dict(request.json_schema),
                "strict": True,
            },
        }
    return {"type": "json_object"}


def _parse_embeddings(response: Mapping[str, Any], *, expected: int) -> list[list[float]]:
    data = response.get("data")
    if not isinstance(data, Sequence):
        raise ProviderInvalidOutputError(
            "The provider returned no embedding data.", provider=_PROVIDER_NAME
        )
    indexed: list[tuple[int, list[float]]] = []
    for position, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise ProviderInvalidOutputError("Malformed embedding entry.", provider=_PROVIDER_NAME)
        vector = item.get("embedding")
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            raise ProviderInvalidOutputError("Malformed embedding vector.", provider=_PROVIDER_NAME)
        index = item.get("index")
        order = index if isinstance(index, int) else position
        indexed.append((order, [float(value) for value in vector]))
    indexed.sort(key=lambda pair: pair[0])
    vectors = [vector for _, vector in indexed]
    if len(vectors) != expected:
        raise ProviderInvalidOutputError(
            "The provider returned an unexpected number of embeddings.", provider=_PROVIDER_NAME
        )
    return vectors


# ---------------------------------------------------------------------------
# HTTP transport (production seam) and provider selection
# ---------------------------------------------------------------------------


class HttpxOpenAITransport(OpenAITransport):
    """A thin OpenAI REST transport built on ``httpx``.

    ``httpx`` is imported lazily so that importing this module never requires
    the dependency and no client is constructed until a call is actually made.
    Network calls happen only in production use, never in tests.
    """

    def __init__(self, *, api_key: str, base_url: str, timeout_seconds: float) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _client(self) -> Any:  # pragma: no cover - exercised only against a live endpoint
        import httpx

        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def _post_json(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        import httpx

        try:
            async with self._client() as client:
                response = await client.post(path, json=dict(payload))
        except httpx.TimeoutException as error:
            raise OpenAITransportError("timeout", "The OpenAI request timed out.") from error
        except httpx.HTTPError as error:
            raise OpenAITransportError("unavailable", "The OpenAI endpoint is unavailable.") from error
        return _raise_for_status(response)

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        return await self._post_json("/chat/completions", payload)

    def chat_stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:  # pragma: no cover
        # Streaming over server-sent events is wired in the assistant streaming
        # task; the non-streaming path is sufficient for the provider contract.
        raise OpenAITransportError("unavailable", "Streaming transport is not configured.")

    async def embeddings(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        return await self._post_json("/embeddings", payload)

    async def transcribe(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        raise OpenAITransportError("unavailable", "Transcription transport is not configured.")

    async def synthesize(self, payload: Mapping[str, Any]) -> bytes:  # pragma: no cover
        raise OpenAITransportError("unavailable", "Speech synthesis transport is not configured.")


def _raise_for_status(response: Any) -> Mapping[str, Any]:  # pragma: no cover - live endpoint only
    status = response.status_code
    if status == 429:
        retry_after = response.headers.get("retry-after")
        raise OpenAITransportError(
            "rate_limited",
            "The OpenAI request was rate limited.",
            retry_after_seconds=float(retry_after) if retry_after else None,
        )
    if status >= 500:
        raise OpenAITransportError("unavailable", "The OpenAI endpoint returned a server error.")
    if status >= 400:
        raise OpenAITransportError("unavailable", f"The OpenAI request failed with status {status}.")
    return cast(Mapping[str, Any], response.json())


def create_ai_provider(
    settings: "Settings",
    *,
    transport: OpenAITransport | None = None,
) -> AIProvider:
    """Select and construct the AI provider from server-side configuration.

    ``AI_PROVIDER=openai`` builds the OpenAI adapter; ``disabled`` yields a
    provider that returns typed unavailable errors. The model and embedding
    settings are resolved here and never leave the server (Requirement 11.3).
    """

    provider = settings.ai_provider
    if provider == "disabled":
        return DisabledAIProvider()
    if provider == "openai":
        active_transport = transport or _default_openai_transport(settings)
        return OpenAIProvider(
            active_transport,
            model=settings.ai_model,
            embedding_model=settings.embedding_model,
            transcription_model=getattr(settings, "ai_transcription_model", None),
            speech_model=getattr(settings, "ai_speech_model", None),
        )
    raise ProviderConfigurationError(f"Unsupported AI provider: {provider}")


def _default_openai_transport(settings: "Settings") -> OpenAITransport:
    api_key = settings.ai_api_key.get_secret_value() if settings.ai_api_key is not None else ""
    if not api_key:
        raise ProviderConfigurationError(
            "AI_API_KEY is required to use the OpenAI provider.", provider=_PROVIDER_NAME
        )
    return HttpxOpenAITransport(
        api_key=api_key,
        base_url=getattr(settings, "ai_base_url", "https://api.openai.com/v1"),
        timeout_seconds=float(getattr(settings, "ai_request_timeout_seconds", 30.0)),
    )
