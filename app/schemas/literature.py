from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class PaperAuthor(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    orcid: str | None = None


class OpenAccessLocation(BaseModel):
    """One known place a paper's full text is hosted.

    Acquisition ranks these by how likely they are to download without a 403:
    repository and preprint copies answer plain HTTP clients, while publisher
    endpoints frequently block them on sight. Fields stay plain ``str`` rather
    than ``HttpUrl`` because this comes straight from a third-party API — one
    malformed URL must not reject the whole paper record.
    """

    #: Best URL this location offers: the PDF when known, else its landing page.
    url: str = Field(min_length=1)
    pdf_url: str | None = None
    landing_page_url: str | None = None
    host_type: str | None = None  # OpenAlex: "repository" | "publisher"
    source_type: str | None = None  # OpenAlex source.type: "repository" | "journal"…
    version: str | None = None  # publishedVersion | acceptedVersion | submittedVersion
    license: str | None = None
    is_oa: bool = False
    #: Set once a download attempt proved the URL permanently gone (404/410), so
    #: later runs stop re-trying a link OpenAlex has not refreshed.
    stale: bool = False


class PaperMetadata(BaseModel):
    stable_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=5_000)
    authors: list[PaperAuthor] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1400, le=2100)
    abstract: str | None = None
    doi: str | None = None
    venue: str | None = None
    publisher: str | None = None
    citation_count: int = Field(default=0, ge=0)
    open_access_url: HttpUrl | None = None
    #: Every hosting location OpenAlex knows (publisher + repositories + preprint
    #: servers). ``open_access_url`` stays as the single-URL compatibility field
    #: for rows stored before this list existed.
    oa_locations: list[OpenAccessLocation] = Field(default_factory=list)
    arxiv_id: str | None = None
    openalex_id: str | None = None
    source: Literal["openalex", "crossref", "arxiv"] = "openalex"
    sources: list[Literal["openalex", "crossref", "arxiv"]] = Field(default_factory=list)
    source_records: list[dict[str, Any]] = Field(default_factory=list)
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
    sources: list[Literal["openalex", "crossref", "arxiv"]] = Field(
        default_factory=lambda: ["openalex", "crossref", "arxiv"]
    )

    @model_validator(mode="after")
    def check_years(self) -> "SearchPapersInput":
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must be less than or equal to year_to")
        return self


class FullTextAvailability(BaseModel):
    """Pre-download guess at whether a paper's PDF can be fetched automatically.

    Shown while the user picks papers, i.e. before any download has been
    attempted, so it must never read as a promise: it only reflects what the
    stored metadata knows about *where* the full text lives. The tiers follow
    the acquisition ranking in ``app.literature.open_access`` — repository and
    preprint copies answer plain HTTP clients, publisher endpoints often answer
    403 — so ``uncertain`` is a real, useful warning rather than noise.
    """

    level: Literal["direct", "likely", "uncertain", "manual"]
    label: str
    detail: str
    #: Host that would be tried first, for the "来源：doi.org" style hint.
    host: str | None = None
    #: How many distinct URLs acquisition would attempt.
    candidates: int = Field(default=0, ge=0)


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
