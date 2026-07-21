"""Deployment-compatible health routes.

The versioned ``/v1/health`` and unversioned ``/health`` routes coexist so
existing infrastructure health checks keep working alongside the versioned API
(Requirement 17.1).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from app.api.serialization import ApiModel

router = APIRouter(tags=["health"])


class HealthResponse(ApiModel):
    status: Literal["ok"]


@router.get("/health", operation_id="get_health_unversioned")
def health_unversioned() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/v1/health", operation_id="get_health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")
