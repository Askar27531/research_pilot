from typing import Any, Literal

from pydantic import BaseModel, Field

TaskType = Literal["literature_search"]


class AgentTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    task_type: TaskType
    objective: str = Field(min_length=3, max_length=4_000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    task_id: str
    project_id: str
    agent: str
    status: Literal["completed", "failed"]
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    loaded_skills: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class SkillMetadata(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=50)
    path: str


class LoadedSkill(SkillMetadata):
    content: str
