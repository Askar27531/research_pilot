"""Project research profile and paper-acquisition models for the main workflow."""

from typing import Literal

from pydantic import BaseModel, Field


class ResearchProfileInput(BaseModel):
    problem_statement: str = Field(min_length=3, max_length=4_000)
    objectives: list[str] = Field(min_length=1, max_length=20)
    baseline: str | None = Field(default=None, max_length=2_000)
    environment_or_data: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=30)
    pain_points: list[str] = Field(default_factory=list, max_length=30)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: int | None = Field(default=None, ge=1)


class ResearchProfile(ResearchProfileInput):
    profile_id: str
    project_id: str
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class PaperAcquisition(BaseModel):
    project_id: str
    paper_id: str
    status: Literal["pending", "downloading", "awaiting_upload", "parsed", "failed"]
    source_url: str | None = None
    document_id: str | None = None
    error: str | None = None
    updated_at: str
