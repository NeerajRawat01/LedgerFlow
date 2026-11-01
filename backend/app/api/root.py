"""Application root endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.system import RootResponse

router = APIRouter(tags=["system"])

_DESCRIPTION = (
    "LedgerFlow backend: AI-powered financial consolidation and reporting "
    "platform for multi-entity, multi-currency groups. Phase 1 provides "
    "application infrastructure only."
)


@router.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    """Return basic application identification."""
    return RootResponse(
        name="LedgerFlow",
        service=settings.service_name,
        version=settings.version,
        phase="1",
        description=_DESCRIPTION,
    )
