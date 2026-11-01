"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.system import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return service health status."""
    return HealthResponse(status="ok", service=settings.service_name)
