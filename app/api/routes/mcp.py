from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/status")
async def mcp_status(request: Request) -> dict[str, Any]:
    """Return redacted discovery, health, routing, and fallback state."""
    return request.app.state.mcp_registry.public_status()
