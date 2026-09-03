import shutil
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings

router = APIRouter(tags=["system"])


class PaperAnalysisHealth(BaseModel):
    ready: bool
    text_model: str
    vision_model: str | None = None
    vision_capable: bool = False
    ocr_available: bool = False
    blockers: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ready", "blocked"]
    service: str = "research-pilot"
    dependencies: dict[str, str]
    paper_analysis: PaperAnalysisHealth


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = get_settings()
    blockers: list[str] = []
    vision_model = settings.ollama_vision_model.strip()
    vision_capable = False
    ollama_status = "unavailable"
    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url.rstrip("/"), timeout=10
        ) as client:
            text_response = await client.post(
                "/api/show", json={"model": settings.ollama_model}
            )
            if text_response.is_error:
                blockers.append(f"文本模型 {settings.ollama_model} 不可用")
            if not vision_model:
                blockers.append("请设置 OLLAMA_VISION_MODEL 后再开始新项目")
            else:
                vision_response = await client.post(
                    "/api/show", json={"model": vision_model}
                )
                if vision_response.is_error:
                    blockers.append(f"视觉模型 {vision_model} 不可用")
                else:
                    capabilities = vision_response.json().get("capabilities") or []
                    vision_capable = "vision" in capabilities
                    if not vision_capable:
                        blockers.append(f"模型 {vision_model} 不支持视觉输入")
            ollama_status = "ready"
    except Exception as exc:  # noqa: BLE001 - health reports dependency failures
        blockers.append(f"无法连接本地模型：{type(exc).__name__}")

    database_status = "ready"
    try:
        async with request.app.state.database.connect() as connection:
            await connection.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - health must not raise for a failed dependency
        database_status = "unavailable"
        blockers.append("数据库当前不可用")

    analysis = PaperAnalysisHealth(
        ready=not blockers,
        text_model=settings.ollama_model,
        vision_model=vision_model or None,
        vision_capable=vision_capable,
        ocr_available=shutil.which("tesseract") is not None,
        blockers=blockers,
    )
    return HealthResponse(
        status="ready" if analysis.ready else "blocked",
        dependencies={"database": database_status, "ollama": ollama_status},
        paper_analysis=analysis,
    )
