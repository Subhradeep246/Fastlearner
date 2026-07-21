from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, create_engine

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestIdMiddleware
from app.api.routers import health as health_router
from app.api.routers import identity as identity_router
from app.auth.identity import IdentityProvider, LocalIdentityProvider
from app.auth.policy import PolicyEngine
from app.auth.sessions import SessionSigner
from app.config import Settings, load_settings
from app.persistence.checks import check_database_url

#: Fixed loopback-only development signing secret used when none is configured.
#: Local auth is forbidden in production (see ``LocalIdentityProvider``), so this
#: default never applies to a deployed environment.
_LOCAL_DEV_SIGNING_SECRET = "local-development-session-signing-secret"


def _lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.database_url is not None:
            check_database_url(settings.database_url.get_secret_value())
        yield

    return lifespan


def _build_engine(settings: Settings) -> Engine | None:
    if settings.database_url is None:
        return None
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def _build_identity_provider(settings: Settings) -> IdentityProvider | None:
    if settings.auth_mode != "local" or settings.environment == "production":
        return None
    secret = (
        settings.session_signing_secret.get_secret_value()
        if settings.session_signing_secret is not None
        else _LOCAL_DEV_SIGNING_SECRET
    )
    return LocalIdentityProvider(SessionSigner(secret), environment=settings.environment)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    identity_provider: IdentityProvider | None = None,
) -> FastAPI:
    runtime_settings = settings or load_settings()
    application = FastAPI(
        title="FastLearner API",
        version="0.1.0",
        lifespan=_lifespan(runtime_settings),
    )
    application.state.settings = runtime_settings
    application.state.engine = engine if engine is not None else _build_engine(runtime_settings)
    application.state.identity_provider = (
        identity_provider
        if identity_provider is not None
        else _build_identity_provider(runtime_settings)
    )
    application.state.policy_engine = PolicyEngine()

    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(application)
    application.include_router(health_router.router)
    application.include_router(identity_router.router)

    return application


app = create_app()
