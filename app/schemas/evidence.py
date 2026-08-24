from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.documents import BoundingBox


class EvidenceNode(BaseModel):
    evidence_id: str
    schema_version: int = 1
    project_id: str
    paper_id: str
    document_id: str
    evidence_type: Literal["text", "figure", "table"]
    claim: str = Field(min_length=3, max_length=4_000)
    confidence: float = Field(ge=0, le=1)
    page_number: int = Field(ge=1)
    section: str | None = None
    label: str | None = None
    excerpt: str | None = Field(default=None, max_length=1_000)
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    bbox: BoundingBox | None = None
    source_path: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str

    @model_validator(mode="after")
    def validate_locator(self) -> "EvidenceNode":
        if self.evidence_type == "text":
            if self.excerpt is None or self.span_start is None or self.span_end is None:
                raise ValueError("Text evidence requires excerpt and span offsets")
            if self.span_end <= self.span_start:
                raise ValueError("span_end must be greater than span_start")
        elif self.label is None:
            raise ValueError("Figure and table evidence require a label")
        return self


class LinkedDocument(BaseModel):
    document_id: str
    project_id: str
    paper_id: str
    sha256: str
    relative_path: str
    created_at: str


class ClaimValue(BaseModel):
    value: str = Field(min_length=1, max_length=4_000)
    kind: Literal["supported", "inference", "suggestion"] = "supported"
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_support(self) -> "ClaimValue":
        if self.kind == "supported" and not self.evidence_ids:
            raise ValueError("Supported claims require at least one evidence_id")
        if self.kind != "supported" and self.evidence_ids:
            raise ValueError("Inference and suggestion values cannot cite evidence as fact")
        return self


class PaperSummary(BaseModel):
    summary_id: str
    schema_version: int = 1
    project_id: str
    paper_id: str
    method: list[ClaimValue] = Field(default_factory=list)
    datasets: list[ClaimValue] = Field(default_factory=list)
    metrics: list[ClaimValue] = Field(default_factory=list)
    contributions: list[ClaimValue] = Field(default_factory=list)
    limitations: list[ClaimValue] = Field(default_factory=list)
    created_at: str


class CompactedContext(BaseModel):
    project_id: str
    paper_id: str
    summary: PaperSummary
    evidence_index: list[EvidenceNode]
    source_characters: int = Field(ge=0)
    compacted_characters: int = Field(ge=0)
    reduction_ratio: float = Field(ge=0, le=1)


class ComparisonCell(BaseModel):
    value: str | None = None
    kind: Literal["supported", "inference", "suggestion", "missing"] = "missing"
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_support(self) -> "ComparisonCell":
        if self.kind == "supported" and (not self.value or not self.evidence_ids):
            raise ValueError("Supported comparison cells require a value and evidence")
        if self.kind == "missing" and (self.value is not None or self.evidence_ids):
            raise ValueError("Missing comparison cells cannot contain a value or evidence")
        return self


class ComparisonRow(BaseModel):
    paper_id: str
    cells: dict[str, ComparisonCell]


class CrossPaperComparison(BaseModel):
    project_id: str
    columns: list[str]
    rows: list[ComparisonRow]


class TextEvidenceCreate(BaseModel):
    paper_id: str
    document_id: str
    page_number: int = Field(ge=1)
    claim: str = Field(min_length=3, max_length=4_000)
    quote: str = Field(min_length=1, max_length=1_000)
    confidence: float = Field(ge=0, le=1)


class FigureEvidenceCreate(BaseModel):
    paper_id: str
    document_id: str
    figure_id: str
    claim: str = Field(min_length=3, max_length=4_000)
    confidence: float = Field(ge=0, le=1)


class TableEvidenceCreate(BaseModel):
    paper_id: str
    document_id: str
    table_id: str
    claim: str = Field(min_length=3, max_length=4_000)
    confidence: float = Field(ge=0, le=1)


class EvidenceSourcePreview(BaseModel):
    evidence: EvidenceNode
    content_type: Literal["text", "image"]
    excerpt: str | None = None
    source_path: str
