from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.literature import PaperMetadata
from app.schemas.research import ResearchRequest
from app.schemas.transfer import PaperAcquisition

ProjectStatus = Literal["created", "running", "waiting", "completed", "failed"]


class ProjectRecord(BaseModel):
    id: str
    name: str
    goal: str
    request: ResearchRequest
    status: ProjectStatus
    current_stage: str
    last_run_id: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class StoredPaper(BaseModel):
    id: str
    project_id: str
    stable_key: str
    metadata: PaperMetadata
    lexical_score: float | None = None
    llm_score: float | None = None
    relevance_score: float | None = None
    selection_reason: str | None = None
    selected: bool
    created_at: datetime
    updated_at: datetime
    acquisition: PaperAcquisition | None = None


class TraceRecord(BaseModel):
    id: str
    trace_id: str
    project_id: str
    event_type: str
    agent: str | None = None
    node: str | None = None
    tool: str | None = None
    success: bool
    latency_ms: int | None = None
    summary: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
