from typing import Any, Literal

from pydantic import BaseModel, Field

BlockRole = Literal["body", "heading", "caption", "header", "footer", "page_number"]
SectionType = Literal[
    "abstract", "background", "method", "experiment", "discussion", "references", "other"
]


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class DocumentBlock(BaseModel):
    """A text block with its typographic role, kept in reading order.

    Roles let downstream stages (chunking, evidence building) drop noise such as
    headers/footers/page numbers and treat captions and headings as boundaries
    instead of mid-paragraph cut points.
    """

    index: int = Field(ge=0)
    role: BlockRole = "body"
    text: str = Field(min_length=1, max_length=8_000)
    bbox: BoundingBox | None = None


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
    blocks: list[DocumentBlock] = Field(default_factory=list)


class DocumentSection(BaseModel):
    title: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    level: int = Field(default=1, ge=1, le=6)
    confidence: float = Field(ge=0, le=1)
    type: SectionType = "other"


class FigureMention(BaseModel):
    """A sentence in the paper prose that references a figure/table by label.

    Backing link for cross-modal grounding: it lets the visual analysis and the
    automatic visual review see what the paper *says* about a figure, instead of
    asking the vision model to interpret a crop with only its caption.
    """

    page_number: int = Field(ge=1)
    sentence: str = Field(min_length=1, max_length=500)


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
    extraction_method: Literal["raster", "vector_region"] = "raster"
    mentions: list[FigureMention] = Field(default_factory=list)


class TableCandidate(BaseModel):
    table_id: str
    document_id: str
    page_number: int = Field(ge=1)
    label: str | None = None
    caption: str | None = None
    bbox: BoundingBox | None = None
    cells: list[list[str | None]] = Field(default_factory=list)
    source_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mentions: list[FigureMention] = Field(default_factory=list)


class VisualObservation(BaseModel):
    figure_type: Literal["architecture", "result", "ablation", "table", "other"]
    summary: str
    observations: list[str] = Field(default_factory=list)
    variables_or_components: list[str] = Field(default_factory=list)
    main_results: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


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


class PaperHandle(BaseModel):
    paper_handle: str
    status: Literal["submitted", "parsed", "failed"]


class PaperAnalysisJob(BaseModel):
    analysis_job_id: str
    paper_handle: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: dict[str, int] = Field(default_factory=dict)
    error: dict[str, str] | None = None
    result: dict[str, Any] | None = None
