"""Authenticated study-assistant and optional speech endpoints."""

from typing import cast

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.adapters.ai import create_ai_provider
from app.config import Settings
from app.domain.ai import GenerationRequest, system_message, user_message
from app.domain.identity import ActorContext
from app.domain.ai import ProviderConfigurationError, ProviderUnavailableError
from app.api.dependencies import get_actor

router = APIRouter(prefix="/v1/assistant", tags=["assistant"])

_SYSTEM = """You are FastLearner, a concise and encouraging study partner.
Explain at the learner's level, prefer active recall, never pretend to know
private context not supplied, and finish with one useful next step."""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)


class ChatResponse(BaseModel):
    message: str
    model: str
    provider: str


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5_000)


@router.get("/status")
def assistant_status(
    request: Request, _actor: ActorContext = Depends(get_actor)
) -> dict[str, object]:
    settings = cast(Settings, request.app.state.settings)
    ai_ready = bool(
        settings.ai_api_key and settings.ai_api_key.get_secret_value().strip()
    )
    speech_ready = bool(
        settings.elevenlabs_api_key
        and settings.elevenlabs_api_key.get_secret_value().strip()
    )
    return {
        "text": settings.ai_provider != "disabled" and ai_ready,
        "provider": settings.ai_provider,
        "model": settings.ai_model,
        "speech": speech_ready,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    body: ChatRequest,
    _actor: ActorContext = Depends(get_actor),
) -> ChatResponse:
    settings = cast(Settings, request.app.state.settings)
    provider = create_ai_provider(settings)
    result = await provider.generate(
        GenerationRequest(
            messages=(system_message(_SYSTEM), user_message(body.message.strip())),
            temperature=0.25,
            max_output_tokens=1_200,
        )
    )
    return ChatResponse(
        message=result.text, model=result.model, provider=settings.ai_provider
    )


@router.post("/speech")
async def synthesize_speech(
    request: Request,
    body: SpeechRequest,
    _actor: ActorContext = Depends(get_actor),
) -> Response:
    settings = cast(Settings, request.app.state.settings)
    if (
        settings.elevenlabs_api_key is None
        or not settings.elevenlabs_api_key.get_secret_value().strip()
    ):
        raise ProviderConfigurationError("ElevenLabs speech is not configured.", provider="elevenlabs")
    key = settings.elevenlabs_api_key.get_secret_value()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.ai_request_timeout_seconds) as client:
            result = await client.post(
                url,
                headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                json={"text": body.text, "model_id": settings.elevenlabs_model_id},
            )
    except httpx.HTTPError as error:
        raise ProviderUnavailableError("ElevenLabs speech is unavailable.", provider="elevenlabs") from error
    if result.status_code >= 400:
        raise ProviderUnavailableError(
            f"ElevenLabs speech failed with status {result.status_code}.", provider="elevenlabs"
        )
    return Response(content=result.content, media_type="audio/mpeg")
