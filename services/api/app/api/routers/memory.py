"""Deliberate, owner-scoped learner memory API."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.adapters.files import SignatureFileScanner
from app.api.dependencies import _engine, get_actor, get_idempotency_key
from app.api.serialization import ApiModel
from app.domain.identity import ActorContext
from app.domain.memory import EpisodeKind
from app.repositories.unit_of_work import unit_of_work
from app.services.memory import MemoryService

router = APIRouter(prefix="/v1/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    kind: EpisodeKind = EpisodeKind.NOTE
    subject_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryResponse(ApiModel):
    id: UUID
    subject_id: UUID | None
    source_id: UUID
    kind: str
    content: str
    visibility: str
    user_confidence: float | None
    status: str


def _service(request: Request) -> MemoryService:
    engine = _engine(request)
    return MemoryService(lambda: unit_of_work(engine), scanner=SignatureFileScanner())


@router.get("", response_model=list[MemoryResponse])
def list_memories(
    request: Request,
    q: str | None = Query(default=None, max_length=500),
    actor: ActorContext = Depends(get_actor),
) -> list[Any]:
    items = _service(request).list_memories(actor)
    if q:
        needle = q.casefold().strip()
        items = [item for item in items if needle in item.content.casefold()]
    return items


@router.post("", response_model=MemoryResponse, status_code=201)
def save_memory(
    request: Request,
    body: MemoryCreate,
    actor: ActorContext = Depends(get_actor),
    key: str = Depends(get_idempotency_key),
) -> Any:
    captured = _service(request).save_context(
        actor,
        content=body.content,
        kind=body.kind,
        subject_id=body.subject_id,
        source_title=body.title,
        user_confidence=body.confidence,
        idempotency_key=key,
    )
    return captured.episode
