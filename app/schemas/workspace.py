from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

MAIN_STAGES = (
    "setup", "searching", "paper_selection", "acquiring_selected",
    "documents_needed", "analyzing_selected", "paused", "analysis_review", "failed",
)


class ProjectSummary(BaseModel):
    id: str
    name: str
    user_stage: Literal[MAIN_STAGES]
    status_label: str
    updated_at: datetime


class WaitAction(BaseModel):
    type: Literal["wait"] = "wait"
    job_id: str | None = None


class MissingDocument(BaseModel):
    paper_id: str | None = None
    upload_token: str | None = None
    title: str
    year: int | None = None
    reason: str | None = None


class UploadDocumentsAction(BaseModel):
    type: Literal["upload_documents"] = "upload_documents"
    documents: list[MissingDocument]


class RetryAction(BaseModel):
    type: Literal["retry"] = "retry"
    reason: str | None = None


NextAction = Annotated[
    WaitAction | UploadDocumentsAction | RetryAction,
    Field(discriminator="type"),
]


class WorkspaceProgress(BaseModel):
    found_papers: int = 0
    full_text_papers: int = 0
    analyzed_pages: int = 0
    analyzed_visuals: int = 0
    completed_items: int = 0
    running_items: int = 0
    failed_items: int = 0
    total_visuals: int = 0
    completed_visuals: int = 0
    current_step: str | None = None
    # Which visual is being analyzed right now: page + figure/table ordinal
    # (drives the fine-grained "第几页 · 第几个图/表" progress line).
    current_visual: dict[str, Any] | None = None


class ProjectWorkspace(BaseModel):
    project_id: str
    name: str
    user_stage: Literal[MAIN_STAGES]
    status_label: str
    status_detail: str
    next_action: NextAction
    progress: WorkspaceProgress
    literature: list[dict[str, Any]] = Field(default_factory=list)
    selected_paper: dict[str, Any] | None = None
    search_plan: dict[str, Any] | None = None
    search_revision: int = 0
    selected_paper_tokens: list[str] = Field(default_factory=list, max_length=2)
    analysis_requirements: str | None = None
    analysis_report: dict[str, Any] | None = None
    # Incremental analysis surface (no longer waits for the final report):
    # - analysis_papers: the selected papers with their live PDF handles
    #   (available as soon as full text is acquired, even before synthesis).
    # - analysis_blocks: per-(paper, part) analysis blocks with status, so the
    #   UI can render each finished part as it lands and show placeholders for
    #   the rest while the run is still going.
    analysis_papers: list[dict[str, Any]] = Field(default_factory=list)
    analysis_blocks: list[dict[str, Any]] = Field(default_factory=list)
    evidence_review: dict[str, int] = Field(default_factory=dict)
    high_risk_review_count: int = 0
    budget_hint: dict[str, Any] | None = None
    # Event-ized HITL (V16): a waiting project that holds an open hitl_event IS
    # `waiting_for_human` (derived view, no extra top-level status).
    waiting_for_human: bool = False
    hitl_events: list[dict[str, Any]] = Field(default_factory=list)
    analysis_board: list[dict[str, Any]] = Field(default_factory=list)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    project_input: dict[str, Any] = Field(default_factory=dict)


class WorkflowJob(BaseModel):
    job_id: str
    project_id: str
    run_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    job_type: Literal["research", "document_analysis"] = "research"
    attempts: int = Field(ge=0)
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ProjectAdvancedInput(BaseModel):
    year_from: int | None = Field(default=None, ge=1400, le=2100)
    year_to: int | None = Field(default=None, ge=1400, le=2100)
    max_papers: int = Field(default=20, ge=1, le=100)
    sources: list[Literal["openalex", "crossref", "arxiv"]] = Field(
        default_factory=lambda: ["openalex", "crossref", "arxiv"]
    )


class WorkspaceProjectCreate(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=120)
    research_question: str = Field(min_length=3, max_length=4_000)
    current_approach: str | None = Field(default=None, max_length=2_000)
    difficulties: list[str] = Field(default_factory=list, max_length=30)
    target_metrics: list[str] = Field(default_factory=list, max_length=30)
    advanced: ProjectAdvancedInput = Field(default_factory=ProjectAdvancedInput)


class WorkspaceProjectUpdate(WorkspaceProjectCreate):
    project_name: str = Field(min_length=1, max_length=120)


class RunWorkspaceAction(BaseModel):
    type: Literal["run"]


class EvidenceReviewWorkspaceAction(BaseModel):
    type: Literal["evidence_review"]
    evidence_token: str = Field(min_length=10)
    status: Literal["confirmed", "doubted", "excluded"]
    note: str | None = Field(default=None, max_length=2_000)


class RegenerateSearchAction(BaseModel):
    type: Literal["regenerate_search"]
    instruction: str | None = Field(default=None, min_length=3, max_length=4_000)


class SelectPapersAction(BaseModel):
    type: Literal["select_papers"]
    paper_tokens: list[str] = Field(min_length=1, max_length=2)
    analysis_requirements: str | None = Field(default=None, max_length=4_000)


class ReanalyzeSelectedAction(BaseModel):
    type: Literal["reanalyze_selected"]


class PauseAnalysisAction(BaseModel):
    type: Literal["pause_analysis"]


class ResumeAnalysisAction(BaseModel):
    type: Literal["resume_analysis"]


class ReviewGateContinueAction(BaseModel):
    """M3 ReviewGate: skip the pre-synthesis review and synthesize as-is."""

    type: Literal["review_gate_continue"]


class ReviewGateRegenerateAction(BaseModel):
    """M3 ReviewGate: the human processed disputed evidence first; clear the
    per-paper analyses so the graph re-runs them before synthesizing."""

    type: Literal["review_gate_regenerate"]


AnalysisPart = Literal["problem", "method", "experiment", "critical", "overview"]


class ReanalyzePartAction(BaseModel):
    """Re-analyze one per-paper block (with an optional extra requirement)."""

    type: Literal["reanalyze_part"]
    paper_token: str = Field(min_length=10)
    part: AnalysisPart
    instruction: str | None = Field(default=None, min_length=1, max_length=2_000)


WorkspaceActionRequest = Annotated[
    RunWorkspaceAction | EvidenceReviewWorkspaceAction | RegenerateSearchAction
    | SelectPapersAction | ReanalyzeSelectedAction
    | PauseAnalysisAction | ResumeAnalysisAction | ReanalyzePartAction
    | ReviewGateContinueAction | ReviewGateRegenerateAction,
    Field(discriminator="type"),
]


class WorkspaceMutationResult(BaseModel):
    message: str
    workspace: ProjectWorkspace
    job: WorkflowJob | None = None
