"""Loopback-only session bootstrap for native and web development clients."""

from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import Engine

from app.api.dependencies import ApiConfigurationError
from app.auth.identity import LocalIdentityProvider, LocalPersona
from app.config import Settings
from app.repositories.identity import SqlIdentityRepository

router = APIRouter(prefix="/v1/local", tags=["local-auth"])


class LocalSessionRequest(BaseModel):
    persona: Literal["learner", "parent", "teacher"] = "learner"


class LocalSessionResponse(BaseModel):
    token: str
    expires_at: str
    persona: str


@router.post("/session", response_model=LocalSessionResponse)
def create_local_session(request: Request, body: LocalSessionRequest) -> LocalSessionResponse:
    settings = cast(Settings, request.app.state.settings)
    if settings.environment == "production" or settings.auth_mode != "local":
        raise ApiConfigurationError("Local session bootstrap is unavailable.")
    engine = cast(Engine | None, request.app.state.engine)
    provider = request.app.state.identity_provider
    if engine is None or not isinstance(provider, LocalIdentityProvider):
        raise ApiConfigurationError("Local session bootstrap is not configured.")
    with engine.begin() as connection:
        issued = provider.issue_local_session(
            SqlIdentityRepository(connection), LocalPersona(body.persona)
        )
    return LocalSessionResponse(
        token=issued.token,
        expires_at=issued.claims.expires_at.isoformat(),
        persona=body.persona,
    )
