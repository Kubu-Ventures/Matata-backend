"""Pydantic response schemas for health check endpoints."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadinessResponse(BaseModel):
    status: str  # "ready" | "degraded"
    checks: Dict[str, str]  # {"postgres": "ok", "redis": "ok", "storage": "ok"}


class WorkerHealthResponse(BaseModel):
    status: str  # "ok" | "alert" | "unavailable"
    queues: Dict[str, int]  # {"gis": 2, "ai": 5, ...}
    alert: bool
