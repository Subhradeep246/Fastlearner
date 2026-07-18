from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import Settings, load_settings
from app.persistence.checks import check_database_url


class HealthResponse(BaseModel):
    status: Literal["ok"]


def _lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.database_url is not None:
            check_database_url(settings.database_url.get_secret_value())
        yield

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or load_settings()
    application = FastAPI(
        title="FastLearner API",
        version="0.1.0",
        lifespan=_lifespan(runtime_settings),
    )
    application.state.settings = runtime_settings
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/v1/health", operation_id="get_health")
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return application


app = create_app()
