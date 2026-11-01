"""Schemas for system/identification endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response payload for GET /api/health."""

    status: str = Field(description="Service health status")
    service: str = Field(description="Service identifier")


class RootResponse(BaseModel):
    """Response payload for GET / (application identification)."""

    name: str = Field(description="Application name")
    service: str = Field(description="Service identifier")
    version: str = Field(description="Application version")
    phase: str = Field(description="Current implementation phase")
    description: str = Field(description="Short application description")
