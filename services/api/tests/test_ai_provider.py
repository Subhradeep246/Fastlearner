"""Unit tests for the vendor-neutral AI port and the OpenAI adapter.

Covers Requirement 11: vendor-neutral generation, streaming, structured output,
embeddings, and optional speech (11.1); OpenAI as the configured provider
(11.2); server-side provider/model selection (11.3); normalized content, usage,
errors, and structured-output validation (11.4); typed provider-unavailable
errors that cannot mutate canonical state (11.5); typed structured-validation
errors (11.6); interchangeable providers (11.7); disabled/offline degradation
(11.8); and language-model output kept out of the source-of-truth role (11.10).

All tests inject a fake transport, so no OpenAI SDK type crosses the adapter
boundary and no real network call is ever made.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping

import pytest

from app.adapters.ai import (
    DisabledAIProvider,
    OpenAIProvider,
    OpenAITransport,
    OpenAITransportError,
    create_ai_provider,
)
from app.config import load_settings
from app.domain.ai import (
    AIProvider,
    FinishReason,
    GenerationRequest,
    GenerationResult,
    ProviderCompleted,
    ProviderConfigurationError,
    ProviderError,
    ProviderInvalidOutputError,
    ProviderRateLimitedError,
    ProviderSafetyError,
    ProviderTextDelta,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SpeechSynthesisRequest,
    StructuredOutputRequest,
    StructuredValidationError,
    TranscriptionRequest,
    Usage,
    user_message,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _collect(iterator: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in iterator]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport(OpenAITransport):
    """A configurable in-memory transport returning OpenAI-shaped dicts."""

    def __init__(
        self,
        *,
        chat_response: Mapping[str, Any] | None = None,
        chat_error: OpenAITransportError | None = None,
        stream_chunks: list[Mapping[str, Any]] | None = None,
        embeddings_response: Mapping[str, Any] | None = None,
        transcribe_response: Mapping[str, Any] | None = None,
        synthesize_audio: bytes | None = None,
    ) -> None:
        self.chat_response = chat_response
        self.chat_error = chat_error
        self.stream_chunks = stream_chunks or []
        self.embeddings_response = embeddings_response
        self.transcribe_response = transcribe_response
        self.synthesize_audio = synthesize_audio
        self.last_payload: Mapping[str, Any] | None = None

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.last_payload = payload
        if self.chat_error is not None:
            raise self.chat_error
        assert self.chat_response is not None
        return self.chat_response

    async def chat_stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        self.last_payload = payload
        if self.chat_error is not None:
            raise self.chat_error
        for chunk in self.stream_chunks:
            yield chunk

    async def embeddings(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.last_payload = payload
        if self.chat_error is not None:
            raise self.chat_error
        assert self.embeddings_response is not None
        return self.embeddings_response

    async def transcribe(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.last_payload = payload
        assert self.transcribe_response is not None
        return self.transcribe_response

    async def synthesize(self, payload: Mapping[str, Any]) -> bytes:
        self.last_payload = payload
        assert self.synthesize_audio is not None
        return self.synthesize_audio


@dataclass
class Intent:
    category: str
    confidence: float


class IntentValidator:
    """A minimal structured validator standing in for a Pydantic model."""

    def validate(self, data: Mapping[str, Any]) -> Intent:
        if "category" not in data:
            raise StructuredValidationError("Missing required field 'category'.")
        return Intent(category=str(data["category"]), confidence=float(data.get("confidence", 0.0)))


def _provider(transport: FakeTransport) -> OpenAIProvider:
    return OpenAIProvider(
        transport,
        model="gpt-neutral",
        embedding_model="embed-neutral",
        transcription_model="transcribe-neutral",
        speech_model="speech-neutral",
    )


def _generation_request() -> GenerationRequest:
    return GenerationRequest(messages=(user_message("Explain equivalent fractions."),))


# ---------------------------------------------------------------------------
# Generation and normalization (Requirements 11.1, 11.4)
# ---------------------------------------------------------------------------


def test_generate_normalizes_content_usage_and_finish_reason() -> None:
    transport = FakeTransport(
        chat_response={
            "model": "gpt-neutral-2025",
            "choices": [{"message": {"role": "assistant", "content": "Two fractions are equal when..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42},
        }
    )
    result = run(_provider(transport).generate(_generation_request()))

    assert isinstance(result, GenerationResult)
    assert result.text.startswith("Two fractions")
    assert result.finish_reason is FinishReason.STOP
    assert result.model == "gpt-neutral-2025"
    assert result.usage == Usage(prompt_tokens=12, completion_tokens=30, total_tokens=42)
    # Server-side model configuration is applied to the outbound payload.
    assert transport.last_payload is not None
    assert transport.last_payload["model"] == "gpt-neutral"


def test_generate_defaults_total_tokens_when_absent() -> None:
    transport = FakeTransport(
        chat_response={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
    )
    result = run(_provider(transport).generate(_generation_request()))
    assert result.finish_reason is FinishReason.LENGTH
    assert result.usage.total_tokens == 12


def test_generate_missing_model_is_configuration_error() -> None:
    provider = OpenAIProvider(FakeTransport(chat_response={}), model=None)
    with pytest.raises(ProviderConfigurationError):
        run(provider.generate(_generation_request()))


# ---------------------------------------------------------------------------
# Streaming (Requirement 11.1)
# ---------------------------------------------------------------------------


def test_stream_yields_text_deltas_then_completion() -> None:
    transport = FakeTransport(
        stream_chunks=[
            {"choices": [{"delta": {"content": "Equiv"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "alent"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        ]
    )
    events = run(_collect(_provider(transport).stream(_generation_request())))

    deltas = [event.text for event in events if isinstance(event, ProviderTextDelta)]
    completions = [event for event in events if isinstance(event, ProviderCompleted)]
    assert deltas == ["Equiv", "alent"]
    assert len(completions) == 1
    assert completions[0].finish_reason is FinishReason.STOP
    assert completions[0].usage.total_tokens == 7


def test_stream_emits_completion_even_without_finish_reason() -> None:
    transport = FakeTransport(stream_chunks=[{"choices": [{"delta": {"content": "hi"}}]}])
    events = run(_collect(_provider(transport).stream(_generation_request())))
    assert any(isinstance(event, ProviderCompleted) for event in events)


def test_stream_maps_transport_failure_to_typed_error() -> None:
    transport = FakeTransport(chat_error=OpenAITransportError("unavailable", "down"))
    with pytest.raises(ProviderUnavailableError):
        run(_collect(_provider(transport).stream(_generation_request())))


# ---------------------------------------------------------------------------
# Structured output (Requirements 11.4, 11.6)
# ---------------------------------------------------------------------------


def _structured_request() -> StructuredOutputRequest[Intent]:
    return StructuredOutputRequest(
        messages=(user_message("Classify this message."),),
        validator=IntentValidator(),
        schema_name="intent",
        json_schema={"type": "object", "properties": {"category": {"type": "string"}}},
    )


def test_structured_output_validates_into_typed_value() -> None:
    transport = FakeTransport(
        chat_response={
            "choices": [{"message": {"content": '{"category": "question_answering", "confidence": 0.9}'}, "finish_reason": "stop"}]
        }
    )
    intent = run(_provider(transport).structured(_structured_request()))
    assert intent == Intent(category="question_answering", confidence=0.9)
    # A JSON-schema response format is requested when a schema is supplied.
    assert transport.last_payload is not None
    assert transport.last_payload["response_format"]["type"] == "json_schema"


def test_structured_output_malformed_json_is_invalid_output_error() -> None:
    transport = FakeTransport(
        chat_response={"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]}
    )
    with pytest.raises(ProviderInvalidOutputError):
        run(_provider(transport).structured(_structured_request()))


def test_structured_output_failing_validation_is_invalid_output_error() -> None:
    transport = FakeTransport(
        chat_response={"choices": [{"message": {"content": '{"confidence": 0.5}'}, "finish_reason": "stop"}]}
    )
    with pytest.raises(ProviderInvalidOutputError):
        run(_provider(transport).structured(_structured_request()))


# ---------------------------------------------------------------------------
# Embeddings (Requirement 11.1)
# ---------------------------------------------------------------------------


def test_embed_returns_vectors_ordered_by_index() -> None:
    transport = FakeTransport(
        embeddings_response={
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
    )
    vectors = run(_provider(transport).embed(["b", "a"]))
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_empty_input_returns_empty_without_transport_call() -> None:
    transport = FakeTransport()
    assert run(_provider(transport).embed([])) == []
    assert transport.last_payload is None


def test_embed_count_mismatch_is_invalid_output_error() -> None:
    transport = FakeTransport(embeddings_response={"data": [{"index": 0, "embedding": [0.1]}]})
    with pytest.raises(ProviderInvalidOutputError):
        run(_provider(transport).embed(["a", "b"]))


# ---------------------------------------------------------------------------
# Typed provider errors that cannot mutate canonical state (Requirement 11.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("unavailable", ProviderUnavailableError),
        ("timeout", ProviderTimeoutError),
        ("rate_limited", ProviderRateLimitedError),
        ("safety", ProviderSafetyError),
    ],
)
def test_transport_errors_map_to_typed_provider_errors(category: str, expected: type[ProviderError]) -> None:
    transport = FakeTransport(chat_error=OpenAITransportError(category, "boom", retry_after_seconds=2.0))
    with pytest.raises(expected) as caught:
        run(_provider(transport).generate(_generation_request()))
    # Typed errors expose a safe payload and never carry provider SDK objects.
    payload = caught.value.safe_payload()
    assert payload["code"] == expected.code
    assert "boom" in payload["message"]


def test_content_filter_finish_reason_is_safety_error() -> None:
    transport = FakeTransport(
        chat_response={"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}
    )
    with pytest.raises(ProviderSafetyError):
        run(_provider(transport).generate(_generation_request()))


def test_rate_limited_error_reports_retry_after() -> None:
    transport = FakeTransport(chat_error=OpenAITransportError("rate_limited", "slow down", retry_after_seconds=5.0))
    with pytest.raises(ProviderRateLimitedError) as caught:
        run(_provider(transport).generate(_generation_request()))
    assert caught.value.safe_payload()["retry_after_seconds"] == 5.0


# ---------------------------------------------------------------------------
# Optional speech (Requirement 11.1)
# ---------------------------------------------------------------------------


def test_transcribe_and_synthesize_round_trip() -> None:
    transport = FakeTransport(
        transcribe_response={"text": "hello there", "model": "transcribe-neutral"},
        synthesize_audio=b"RIFFfakeaudio",
    )
    provider = _provider(transport)
    transcription = run(provider.transcribe(TranscriptionRequest(audio=b"\x00\x01", content_type="audio/wav")))
    assert transcription.text == "hello there"

    speech = run(provider.synthesize(SpeechSynthesisRequest(text="hi", audio_format="mp3")))
    assert speech.audio == b"RIFFfakeaudio"
    assert speech.content_type == "audio/mp3"


# ---------------------------------------------------------------------------
# Provider selection, disabled degradation, and interchangeability
# ---------------------------------------------------------------------------


def _base_settings(**overrides: Any):
    values: dict[str, Any] = {
        "environment": "development",
        "auth_mode": "local",
        "api_host": "127.0.0.1",
        "ai_provider": "openai",
        "ai_model": "gpt-neutral",
        "ai_api_key": "server-side-key",
        "embedding_model": "embed-neutral",
    }
    values.update(overrides)
    return load_settings(**values)


def test_create_ai_provider_selects_openai_from_config() -> None:
    provider = create_ai_provider(_base_settings(), transport=FakeTransport())
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai"


def test_create_ai_provider_selects_disabled_provider() -> None:
    provider = create_ai_provider(_base_settings(ai_provider="disabled"))
    assert isinstance(provider, DisabledAIProvider)
    assert provider.name == "disabled"


def test_openai_selection_requires_api_key() -> None:
    with pytest.raises(ProviderConfigurationError):
        create_ai_provider(_base_settings(ai_api_key=None))


def test_disabled_provider_reports_unavailable_for_every_operation() -> None:
    provider = DisabledAIProvider()
    with pytest.raises(ProviderUnavailableError):
        run(provider.generate(_generation_request()))
    with pytest.raises(ProviderUnavailableError):
        run(provider.embed(["x"]))
    with pytest.raises(ProviderUnavailableError):
        run(_collect(provider.stream(_generation_request())))


def test_alternate_provider_satisfies_the_same_port() -> None:
    # A second provider implementing the neutral port is interchangeable
    # without any change to callers (Requirement 11.7).
    class EchoProvider:
        @property
        def name(self) -> str:
            return "echo"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            return GenerationResult(
                text=request.messages[-1].content,
                usage=Usage(),
                finish_reason=FinishReason.STOP,
                model="echo",
            )

        async def stream(self, request: GenerationRequest) -> AsyncIterator[Any]:
            yield ProviderCompleted(finish_reason=FinishReason.STOP, usage=Usage())

        async def structured(self, request: StructuredOutputRequest[Any]) -> Any:
            return request.validator.validate({"category": "general_chat"})

        async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    provider = EchoProvider()
    assert isinstance(provider, AIProvider)
    result = run(provider.generate(_generation_request()))
    assert result.text == "Explain equivalent fractions."


def test_generation_result_is_a_plain_domain_value() -> None:
    # Language-model output is returned as a normalized result value only; it
    # is never persisted by the adapter (Requirement 11.10).
    transport = FakeTransport(
        chat_response={"choices": [{"message": {"content": "draft"}, "finish_reason": "stop"}], "usage": {}}
    )
    result = run(_provider(transport).generate(_generation_request()))
    assert type(result) is GenerationResult
    assert isinstance(result.usage, Usage)
