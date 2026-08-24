from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkItemProgress(BaseModel):
    item_id: str
    project_id: str
    run_scope: str
    item_key: str
    item_type: str
    status: Literal["running", "completed", "failed"]
    attempts: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    created_at: str
    updated_at: str


class TraceMetrics(BaseModel):
    project_id: str
    total_events: int = Field(ge=0)
    successful_events: int = Field(ge=0)
    failed_events: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    average_latency_ms: float | None = Field(default=None, ge=0)
    recovery_count: int = Field(ge=0)
    by_event_type: dict[str, int]


class ProgressMetrics(BaseModel):
    project_id: str
    total_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    running_items: int = Field(ge=0)
    recovered_items: int = Field(ge=0)
