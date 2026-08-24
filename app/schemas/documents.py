from typing import Literal

from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class DocumentEntry(BaseModel):
    document_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    original_name: str
    size_bytes: int = Field(ge=1)


class WorkspaceManifest(BaseModel):
    project_id: str
    documents: list[DocumentEntry] = Field(default_factory=list)
    generated_at: str
    schema_version: int = 1


class DocumentPage(BaseModel):
    document_id: str
    page_number: int = Field(ge=1)
    text: str
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int
    screenshot_path: str | None = None
    error: str | None = None


class DocumentSection(BaseModel):
    title: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    level: int = Field(default=1, ge=1, le=6)
    confidence: float = Field(ge=0, le=1)


class DocumentFigure(BaseModel):
    figure_id: str
    document_id: str
    page_number: int = Field(ge=1)
    label: str | None = None
    caption: str | None = None
    figure_type: Literal["architecture", "result", "ablation", "other"]
    bbox: BoundingBox
    source_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TableCandidate(BaseModel):
    table_id: str
    document_id: str
    page_number: int = Field(ge=1)
    label: str | None = None
    caption: str | None = None
    bbox: BoundingBox | None = None


class ParsedDocument(BaseModel):
    document_id: str
    project_id: str
    title: str | None = None
    author: str | None = None
    page_count: int = Field(ge=0)
    pages: list[DocumentPage]
    sections: list[DocumentSection]
    figures: list[DocumentFigure]
    tables: list[TableCandidate]
    warnings: list[str] = Field(default_factory=list)


class ParseDocumentInput(BaseModel):
    project_id: str
    document_id: str


class PageInput(ParseDocumentInput):
    page_number: int = Field(ge=1)


class FigureInput(ParseDocumentInput):
    figure_id: str
