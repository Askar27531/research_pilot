from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AnalysisClaim(BaseModel):
    value: str = Field(min_length=1, max_length=4_000)
    kind: Literal["supported", "inference"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def evidence_matches_kind(self) -> "AnalysisClaim":
        if self.kind == "supported" and not self.evidence_ids:
            raise ValueError("Supported analysis claims require evidence")
        if self.kind == "inference" and self.evidence_ids:
            raise ValueError("Inference claims cannot cite evidence as fact")
        return self


class PaperAnalysisDraft(BaseModel):
    overview: AnalysisClaim | None = None
    core_problem: list[AnalysisClaim] = Field(min_length=1, max_length=10)
    methods: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    mechanisms: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    experimental_setup: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    main_results: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    limitations: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    relevance_to_topic: list[AnalysisClaim] = Field(default_factory=list, max_length=20)


class ProblemAnalysisDraft(BaseModel):
    core_problem: list[AnalysisClaim] = Field(min_length=1, max_length=6)
    relevance_to_topic: list[AnalysisClaim] = Field(min_length=1, max_length=6)


class MethodAnalysisDraft(BaseModel):
    methods: list[AnalysisClaim] = Field(min_length=1, max_length=8)
    mechanisms: list[AnalysisClaim] = Field(min_length=1, max_length=10)


class ExperimentAnalysisDraft(BaseModel):
    experimental_setup: list[AnalysisClaim] = Field(min_length=1, max_length=8)
    main_results: list[AnalysisClaim] = Field(min_length=1, max_length=10)


class CriticalAnalysisDraft(BaseModel):
    limitations: list[AnalysisClaim] = Field(min_length=1, max_length=8)


class PaperOverviewDraft(BaseModel):
    overview: AnalysisClaim


class PaperAnalysis(PaperAnalysisDraft):
    paper_id: str
    title: str


class ComparisonDraft(BaseModel):
    overview: AnalysisClaim | None = None
    commonalities: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    differences: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    complementarities: list[AnalysisClaim] = Field(default_factory=list, max_length=20)
    applicability: list[AnalysisClaim] = Field(default_factory=list, max_length=20)


class AnalysisReport(BaseModel):
    project_id: str
    search_revision: int = Field(ge=1)
    analysis_requirements: str | None = None
    papers: list[PaperAnalysis] = Field(min_length=1, max_length=2)
    comparison: ComparisonDraft | None = None
    created_at: str
