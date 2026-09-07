"""Decision-desk request/response models (HITL lean plan)."""

from typing import Literal

from pydantic import BaseModel, Field

ReviewStatus = Literal["confirmed", "doubted", "excluded"]


class DeskClaimRef(BaseModel):
    """One supported conclusion that cites the evidence under review."""

    scope: Literal["paper", "comparison"]
    paper_title: str
    section_label: str
    value: str
    evidence_count: int = Field(ge=1)
    will_downgrade: bool = False


class ReviewDeskItem(BaseModel):
    """One actionable evidence row in the desk queue."""

    evidence_token: str
    resource_url: str
    evidence_id: str
    type: Literal["text", "figure", "table"]
    label: str | None = None
    page: int = Field(ge=1)
    claim: str
    excerpt: str | None = None
    confidence: float = Field(ge=0, le=1)
    review_status: str | None = None
    review_source: str | None = None
    review_note: str | None = None
    cited_by: int = Field(ge=0)
    supports_comparison: bool = False
    priority: float = Field(ge=0, le=1)
    citing: list[DeskClaimRef] = Field(default_factory=list)


class ReviewDeskStats(BaseModel):
    high_risk: int = 0
    high_impact: int = 0
    actionable: int = 0
    excluded: int = 0


class ReviewDesk(BaseModel):
    project_id: str
    segment: Literal["priority", "all"] = "priority"
    stats: ReviewDeskStats
    items: list[ReviewDeskItem] = Field(default_factory=list)


class ReviewPreviewRequest(BaseModel):
    evidence_token: str = Field(min_length=10)
    status: ReviewStatus


class ReviewPreviewResult(BaseModel):
    """Impact echo: what committing ``status`` for this evidence would change."""

    evidence_token: str
    status: ReviewStatus
    cited_total: int = Field(ge=0)
    supports_comparison: bool = False
    downgrade: list[DeskClaimRef] = Field(default_factory=list)
    retained: list[DeskClaimRef] = Field(default_factory=list)
    message: str
