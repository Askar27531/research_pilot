from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class PaperAuthor(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    orcid: str | None = None


class PaperMetadata(BaseModel):
    stable_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=5_000)
    authors: list[PaperAuthor] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1400, le=2100)
    abstract: str | None = None
    doi: str | None = None
    venue: str | None = None
    citation_count: int = Field(default=0, ge=0)
    open_access_url: HttpUrl | None = None
    source: Literal["openalex"] = "openalex"
    source_queries: list[str] = Field(default_factory=list)

    @field_validator("doi")
    @classmethod
    def normalize_doi_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        return normalized.rstrip(" .") or None

    @field_validator("source_queries")
    @classmethod
    def unique_queries(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class SearchPapersInput(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    year_from: int | None = Field(default=None, ge=1400, le=2100)
    year_to: int | None = Field(default=None, ge=1400, le=2100)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def check_years(self) -> "SearchPapersInput":
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must be less than or equal to year_to")
        return self


class SearchResult(BaseModel):
    query: str
    papers: list[PaperMetadata]
    total_available: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    source_latency_ms: int = Field(ge=0)


class SearchQuery(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    purpose: Literal[
        "core",
        "synonym",
        "method",
        "application",
        "high_precision",
        "synonym_expansion",
        "method_expansion",
    ]
    concepts: list[str] = Field(min_length=1, max_length=20)


class SearchQueryPlan(BaseModel):
    topic: str = ""
    required_concept_groups: list[list[str]] = Field(default_factory=list, max_length=10)
    excluded_topics: list[str] = Field(default_factory=list, max_length=20)
    queries: list[SearchQuery] = Field(min_length=3, max_length=5)


class PaperRelevance(BaseModel):
    stable_id: str
    relevance: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1_000)
    matched_aspects: list[str] = Field(default_factory=list, max_length=20)


class PaperRelevanceBatch(BaseModel):
    scores: list[PaperRelevance]


class PaperScreeningDecision(BaseModel):
    stable_id: str
    include: bool
    relevance: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=500)
    matched_required_concepts: list[str] = Field(default_factory=list, max_length=20)


class PaperScreeningBatch(BaseModel):
    decisions: list[PaperScreeningDecision]


class RankedPaper(BaseModel):
    paper: PaperMetadata
    lexical_score: float = Field(ge=0, le=1)
    llm_score: float | None = Field(default=None, ge=0, le=1)
    final_score: float = Field(ge=0, le=1)
    selection_reason: str
    matched_aspects: list[str] = Field(default_factory=list)
