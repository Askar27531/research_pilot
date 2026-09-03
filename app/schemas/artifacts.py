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
