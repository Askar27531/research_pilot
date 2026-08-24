from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalTask(BaseModel):
    task_id: str
    category: Literal["search", "figure", "evidence", "experiment"]
    input: dict[str, Any]
    reference: dict[str, Any]
    prediction: dict[str, Any]
    scoring: str


class EvalDataset(BaseModel):
    name: str
    version: str
    tasks: list[EvalTask] = Field(min_length=20)
    ablations: dict[str, Any]


class EvalItemResult(BaseModel):
    task_id: str
    category: str
    score: float = Field(ge=0, le=1)
    passed: bool
    metrics: dict[str, float]


class EvalRun(BaseModel):
    run_id: str
    dataset_name: str
    dataset_version: str
    started_at: str
    git_commit: str
    model_name: str
    model_digest: str
    parameters: dict[str, Any]
    environment: dict[str, str]
    results: list[EvalItemResult]
    aggregate: dict[str, float]
    ablations: dict[str, Any]
