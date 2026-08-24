from typing import Literal

from pydantic import BaseModel, Field

ArtifactType = Literal["markdown", "csv", "mermaid"]


class ArtifactRecord(BaseModel):
    artifact_id: str
    project_id: str
    artifact_type: ArtifactType
    name: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=1)
    size_bytes: int = Field(ge=0)
    created_at: str


class MarkdownArtifactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    sections: dict[str, str]


class CSVArtifactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    columns: list[str] = Field(min_length=1, max_length=100)
    rows: list[list[str]] = Field(default_factory=list, max_length=10_000)


class MermaidArtifactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    diagram: str = Field(min_length=3, max_length=100_000)
