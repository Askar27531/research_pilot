from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.schemas.artifacts import ArtifactRecord
from app.schemas.experiments import ExperimentProposal, ExperimentUpdate
from app.schemas.transfer import TransferCandidateSet


class ProjectSummary(BaseModel):
    id: str
    name: str
    user_stage: Literal[
        "setup", "searching", "paper_selection", "acquiring_selected",
        "documents_needed", "analyzing_selected", "analysis_review",
        "direction_review", "researching", "experiment_review", "failed",
    ]
    status_label: str
    updated_at: datetime


class StartAction(BaseModel):
    type: Literal["start"] = "start"


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


class ReviewDirectionAction(BaseModel):
    type: Literal["review_direction"] = "review_direction"


class ReviewExperimentAction(BaseModel):
    type: Literal["review_experiment"] = "review_experiment"


class DownloadAction(BaseModel):
    type: Literal["download"] = "download"


class RetryAction(BaseModel):
    type: Literal["retry"] = "retry"
    reason: str | None = None


NextAction = Annotated[
    StartAction | WaitAction | UploadDocumentsAction | ReviewDirectionAction
    | ReviewExperimentAction | DownloadAction | RetryAction,
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


class ProjectWorkspace(BaseModel):
    project_id: str
    name: str
    user_stage: Literal[
        "setup", "searching", "paper_selection", "acquiring_selected",
        "documents_needed", "analyzing_selected", "analysis_review",
        "direction_review", "researching", "experiment_review", "failed",
    ]
    status_label: str
    status_detail: str
    next_action: NextAction
    progress: WorkspaceProgress
    direction: TransferCandidateSet | None = None
    experiment: ExperimentProposal | None = None
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    literature: list[dict[str, Any]] = Field(default_factory=list)
    selected_paper: dict[str, Any] | None = None
    search_plan: dict[str, Any] | None = None
    search_revision: int = 0
    selected_paper_tokens: list[str] = Field(default_factory=list, max_length=2)
    analysis_requirements: str | None = None
    analysis_report: dict[str, Any] | None = None
    evidence_review: dict[str, int] = Field(default_factory=dict)
    output_freshness: Literal["fresh", "stale"] = "fresh"
    resources: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    project_input: dict[str, Any] = Field(default_factory=dict)


class WorkflowJob(BaseModel):
    job_id: str
    project_id: str
    run_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    job_type: Literal["research", "document_analysis", "resynthesis"] = "research"
    attempts: int = Field(ge=0)
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class RevisionPreviewRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=8_000)


class ExperimentRevisionDraft(BaseModel):
    summary: str = Field(min_length=3, max_length=4_000)
    experiment_updates: list[ExperimentUpdate] = Field(min_length=1, max_length=20)


class RevisionPreview(BaseModel):
    preview_id: str
    project_id: str
    target: Literal["transfer", "experiment"]
    base_version: int = Field(ge=1)
    instruction: str
    summary: str
    patch: dict[str, Any]
    status: Literal["pending", "applied", "cancelled"]
    created_at: datetime
    updated_at: datetime


class RevisionPreviewApplyResponse(BaseModel):
    preview: RevisionPreview
    result: dict[str, Any]


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


class DecisionWorkspaceAction(BaseModel):
    type: Literal["direction_decision", "plan_decision"]
    decision: Literal["accept", "reject"]


class PreviewWorkspaceAction(BaseModel):
    type: Literal["direction_revision_preview", "plan_revision_preview"]
    instruction: str = Field(min_length=3, max_length=8_000)


class ApplyWorkspaceAction(BaseModel):
    type: Literal["direction_revision_apply", "plan_revision_apply"]
    preview_token: str = Field(min_length=10)


class EvidenceReviewWorkspaceAction(BaseModel):
    type: Literal["evidence_review"]
    evidence_token: str = Field(min_length=10)
    status: Literal["confirmed", "doubted", "excluded"]
    note: str | None = Field(default=None, max_length=2_000)


class RefreshWorkspaceAction(BaseModel):
    type: Literal["refresh_results"]


class RegenerateSearchAction(BaseModel):
    type: Literal["regenerate_search"]
    instruction: str | None = Field(default=None, min_length=3, max_length=4_000)


class ReselectPapersAction(BaseModel):
    type: Literal["reselect_papers"]


class SelectPapersAction(BaseModel):
    type: Literal["select_papers"]
    paper_tokens: list[str] = Field(min_length=1, max_length=2)
    analysis_requirements: str | None = Field(default=None, max_length=4_000)


class ReanalyzeSelectedAction(BaseModel):
    type: Literal["reanalyze_selected"]


WorkspaceActionRequest = Annotated[
    RunWorkspaceAction | DecisionWorkspaceAction | PreviewWorkspaceAction
    | ApplyWorkspaceAction | EvidenceReviewWorkspaceAction | RefreshWorkspaceAction
    | RegenerateSearchAction | ReselectPapersAction | SelectPapersAction
    | ReanalyzeSelectedAction,
    Field(discriminator="type"),
]


class WorkspaceMutationResult(BaseModel):
    message: str
    workspace: ProjectWorkspace
    job: WorkflowJob | None = None
    preview: dict[str, Any] | None = None
