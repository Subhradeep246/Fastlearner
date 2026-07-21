"""Owner-scoped schoolwork API."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.dependencies import _engine, get_actor, get_idempotency_key
from app.api.serialization import ApiModel
from app.domain.identity import ActorContext
from app.domain.work import AssignmentAction, GoalStatus, SubjectKind
from app.services.work import WorkService

router = APIRouter(prefix="/v1", tags=["work"])


class SubjectCreate(BaseModel):
    slug: str
    title: str
    kind: SubjectKind = SubjectKind.LEARNER_CREATED


class AssignmentCreate(BaseModel):
    subject_id: UUID
    title: str
    due_at: datetime
    estimated_minutes: int = Field(gt=0)


class AssignmentUpdate(BaseModel):
    subject_id: UUID | None = None
    title: str | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, gt=0)


class AssignmentActionBody(BaseModel):
    action: AssignmentAction


class GoalCreate(BaseModel):
    title: str
    subject_id: UUID | None = None
    target_at: datetime | None = None


class GoalStatusBody(BaseModel):
    status: GoalStatus


class EffortCreate(BaseModel):
    minutes: int = Field(gt=0)


class WorkResponse(ApiModel):
    id: UUID
    owner_user_id: UUID | None = None
    subject_id: UUID | None = None
    slug: str | None = None
    title: str
    kind: str | None = None
    status: str
    due_at: datetime | None = None
    target_at: datetime | None = None
    estimated_minutes: int | None = None
    brief_source_id: UUID | None = None


def _service(request: Request) -> WorkService:
    return WorkService(_engine(request))


@router.get("/subjects", response_model=list[WorkResponse])
def list_subjects(request: Request, actor: ActorContext = Depends(get_actor)) -> list[Any]:
    return _service(request).list_subjects(actor, include_archived=True)


@router.post("/subjects", response_model=WorkResponse, status_code=201)
def create_subject(
    request: Request,
    body: SubjectCreate,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).create_subject(
        actor, slug=body.slug, title=body.title, kind=body.kind,
        request_id=request.state.request_id,
    )


@router.get("/assignments", response_model=list[WorkResponse])
def list_assignments(request: Request, actor: ActorContext = Depends(get_actor)) -> list[Any]:
    return _service(request).list_assignments(actor)


@router.post("/assignments", response_model=WorkResponse, status_code=201)
def create_assignment(
    request: Request,
    body: AssignmentCreate,
    actor: ActorContext = Depends(get_actor),
    key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).create_assignment(
        actor, **body.model_dump(), idempotency_key=key,
        request_id=request.state.request_id,
    )


@router.patch("/assignments/{assignment_id}", response_model=WorkResponse)
def update_assignment(
    request: Request,
    assignment_id: UUID,
    body: AssignmentUpdate,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).edit_assignment(
        actor, assignment_id, **body.model_dump(), request_id=request.state.request_id
    )


@router.post("/assignments/{assignment_id}/actions", response_model=WorkResponse)
def act_on_assignment(
    request: Request,
    assignment_id: UUID,
    body: AssignmentActionBody,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    service = _service(request)
    action = {
        AssignmentAction.START: service.start_assignment,
        AssignmentAction.COMPLETE: service.complete_assignment,
        AssignmentAction.ARCHIVE: service.archive_assignment,
    }[body.action]
    return action(actor, assignment_id, request_id=request.state.request_id)


@router.delete("/assignments/{assignment_id}", response_model=WorkResponse)
def delete_assignment(
    request: Request,
    assignment_id: UUID,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).delete_assignment(
        actor, assignment_id, request_id=request.state.request_id
    )


@router.post("/assignments/{assignment_id}/effort", status_code=204)
def record_effort(
    request: Request,
    assignment_id: UUID,
    body: EffortCreate,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> None:
    _service(request).record_effort(
        actor, assignment_id=assignment_id, minutes=body.minutes,
        request_id=request.state.request_id,
    )


@router.get("/goals", response_model=list[WorkResponse])
def list_goals(request: Request, actor: ActorContext = Depends(get_actor)) -> list[Any]:
    return _service(request).list_goals(actor)


@router.post("/goals", response_model=WorkResponse, status_code=201)
def create_goal(
    request: Request,
    body: GoalCreate,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).create_goal(
        actor, **body.model_dump(), request_id=request.state.request_id
    )


@router.patch("/goals/{goal_id}/status", response_model=WorkResponse)
def update_goal_status(
    request: Request,
    goal_id: UUID,
    body: GoalStatusBody,
    actor: ActorContext = Depends(get_actor),
    _key: str = Depends(get_idempotency_key),
) -> Any:
    return _service(request).set_goal_status(
        actor, goal_id, body.status, request_id=request.state.request_id
    )
