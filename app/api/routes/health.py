from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class DependencyHealth(BaseModel):
    ollama_configured: bool
    openalex_api_key_configured: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="research-pilot")


@router.get("/health/dependencies", response_model=DependencyHealth)
async def dependency_health() -> DependencyHealth:
    settings = get_settings()
    return DependencyHealth(
        ollama_configured=bool(settings.ollama_model.strip()),
        openalex_api_key_configured=bool((settings.openalex_api_key or "").strip()),
    )
