"""Published curriculum, practice, and transactional mastery API."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, select

from app.api.dependencies import _engine, get_actor, get_idempotency_key
from app.domain.identity import ActorContext, NotFoundError, ValidationError
from app.persistence.models import concepts, content_items, mastery_state, question_versions
from app.services.learning import LearningService

router = APIRouter(prefix="/v1/learning", tags=["learning"])


class ConceptResponse(BaseModel):
    id: UUID
    subject_id: UUID
    title: str
    lesson_title: str | None = None
    lesson_body: str | None = None
    mastery: float = 0.0


class QuestionResponse(BaseModel):
    id: UUID
    concept_id: UUID
    prompt: str
    choices: list[str]


class AttemptRequest(BaseModel):
    question_id: UUID
    answer_index: int = Field(ge=0)
    duration_ms: int = Field(default=0, ge=0)
    hint_used: bool = False
    retry_count: int = Field(default=0, ge=0)


class AttemptResponse(BaseModel):
    correct: bool
    explanation: str
    mastery: float
    recommendation: str
    reason: str


@router.get("/concepts", response_model=list[ConceptResponse])
def list_concepts(
    request: Request, actor: ActorContext = Depends(get_actor)
) -> list[ConceptResponse]:
    with _engine(request).connect() as connection:
        rows = connection.execute(
            select(
                concepts.c.id,
                concepts.c.subject_id,
                concepts.c.title,
                content_items.c.title.label("lesson_title"),
                content_items.c.body.label("lesson_body"),
                mastery_state.c.probability,
            )
            .outerjoin(
                content_items,
                and_(
                    content_items.c.concept_id == concepts.c.id,
                    content_items.c.kind == "lesson",
                    content_items.c.status == "published",
                ),
            )
            .outerjoin(
                mastery_state,
                and_(
                    mastery_state.c.concept_id == concepts.c.id,
                    mastery_state.c.owner_user_id == actor.owner_id,
                ),
            )
            .where(concepts.c.status == "published")
            .order_by(concepts.c.created_at, concepts.c.title)
        ).all()
    return [
        ConceptResponse(
            id=row.id,
            subject_id=row.subject_id,
            title=row.title,
            lesson_title=row.lesson_title,
            lesson_body=row.lesson_body,
            mastery=float(row.probability or 0),
        )
        for row in rows
    ]


@router.get("/concepts/{concept_id}/questions", response_model=list[QuestionResponse])
def list_questions(
    request: Request,
    concept_id: UUID,
    _actor: ActorContext = Depends(get_actor),
) -> list[QuestionResponse]:
    with _engine(request).connect() as connection:
        rows = connection.execute(
            select(question_versions.c.id, question_versions.c.concept_id,
                   question_versions.c.prompt, question_versions.c.answer_spec)
            .where(
                and_(question_versions.c.concept_id == concept_id,
                     question_versions.c.status == "published")
            )
            .order_by(question_versions.c.question_key)
        ).all()
    return [QuestionResponse(id=row.id, concept_id=row.concept_id, prompt=row.prompt,
                             choices=list((row.answer_spec or {}).get("choices", []))) for row in rows]


@router.post("/attempts", response_model=AttemptResponse, status_code=201)
def record_attempt(
    request: Request,
    body: AttemptRequest,
    actor: ActorContext = Depends(get_actor),
    key: str = Depends(get_idempotency_key),
) -> AttemptResponse:
    with _engine(request).connect() as connection:
        row = connection.execute(
            select(question_versions).where(
                and_(question_versions.c.id == body.question_id,
                     question_versions.c.status == "published")
            )
        ).first()
    if row is None:
        raise NotFoundError("The requested question was not found.")
    spec: dict[str, Any] = dict(row.answer_spec or {})
    choices = list(spec.get("choices", []))
    if body.answer_index >= len(choices):
        raise ValidationError("Answer index is outside the available choices.", field="answer_index")
    correct = body.answer_index == int(spec.get("correct_index", -1))
    result = LearningService(_engine(request)).record_learning_event(
        actor,
        concept_id=row.concept_id,
        question_version_id=row.id,
        correct=correct,
        duration_ms=body.duration_ms,
        hint_used=body.hint_used,
        retry_count=body.retry_count,
        idempotency_key=key,
        request_id=request.state.request_id,
    )
    return AttemptResponse(
        correct=correct,
        explanation=row.explanation,
        mastery=float(result.mastery.probability),
        recommendation=result.recommendation.kind,
        reason=result.recommendation.reason,
    )
