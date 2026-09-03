from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ResearchProfileInput(BaseModel):
    problem_statement: str = Field(min_length=3, max_length=4_000)
    objectives: list[str] = Field(min_length=1, max_length=20)
    baseline: str | None = Field(default=None, max_length=2_000)
    environment_or_data: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=30)
    pain_points: list[str] = Field(default_factory=list, max_length=30)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: int | None = Field(default=None, ge=1)


class ResearchProfile(ResearchProfileInput):
    profile_id: str
    project_id: str
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class SupportedMethodField(BaseModel):
    value: str = Field(min_length=1, max_length=4_000)
    kind: Literal["supported_fact", "inference"]
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence(self) -> "SupportedMethodField":
        if self.kind == "supported_fact" and not self.evidence_ids:
            raise ValueError("Supported method facts require evidence")
        if self.kind == "inference" and self.evidence_ids:
            raise ValueError("Inferences cannot cite evidence as established fact")
        return self


class MethodCardDraft(BaseModel):
    paper_id: str
    method_name: SupportedMethodField
    target_problem: SupportedMethodField
    mechanism_steps: list[SupportedMethodField] = Field(min_length=1, max_length=20)
    inputs: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    outputs: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    key_components: list[SupportedMethodField] = Field(default_factory=list, max_length=30)
    assumptions: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    resource_requirements: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    evaluation_context: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    strengths: list[SupportedMethodField] = Field(default_factory=list, max_length=20)
    limitations: list[SupportedMethodField] = Field(default_factory=list, max_length=20)


class MethodCard(MethodCardDraft):
    card_id: str
    project_id: str
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class TransferAssessmentDraft(BaseModel):
    method_card_id: str
    target_challenge: str = Field(min_length=3, max_length=2_000)
    decision: Literal["adapt", "combine", "reject"]
    reusable_components: list[str] = Field(default_factory=list, max_length=20)
    required_adaptations: list[str] = Field(default_factory=list, max_length=20)
    applicability_conditions: list[str] = Field(default_factory=list, max_length=20)
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    expected_benefits: list[str] = Field(default_factory=list, max_length=20)
    failure_risks: list[str] = Field(min_length=1, max_length=20)
    falsification_tests: list[str] = Field(min_length=1, max_length=20)
    mechanism_compatibility: float = Field(ge=0, le=1)
    topic_relevance: float = Field(ge=0, le=1)
    implementation_feasibility: float = Field(ge=0, le=1)
    evidence_sufficiency: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class CombinationCandidateDraft(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    method_card_ids: list[str] = Field(min_length=2, max_length=10)
    target_challenge: str = Field(min_length=3, max_length=2_000)
    integration_design: list[str] = Field(min_length=1, max_length=20)
    information_flow: list[str] = Field(min_length=1, max_length=20)
    objective_changes: list[str] = Field(default_factory=list, max_length=20)
    conflict_resolutions: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    expected_benefits: list[str] = Field(default_factory=list, max_length=20)
    falsification_tests: list[str] = Field(min_length=1, max_length=20)
    evidence_ids: list[str] = Field(min_length=2)
    novelty_risk: Literal["low", "medium", "high"]


class TransferCandidateSetDraft(BaseModel):
    assessments: list[TransferAssessmentDraft] = Field(min_length=1, max_length=50)
    combinations: list[CombinationCandidateDraft] = Field(default_factory=list, max_length=3)


class TransferAssessment(TransferAssessmentDraft):
    assessment_id: str


class CombinationCandidate(CombinationCandidateDraft):
    candidate_id: str


class TransferCandidateSet(BaseModel):
    candidate_set_id: str
    project_id: str
    revision: int = Field(ge=1)
    status: Literal["pending", "accepted", "modified", "rejected"]
    assessments: list[TransferAssessment]
    combinations: list[CombinationCandidate]
    created_at: str
    updated_at: str
    decision_feedback: str | None = None


class TransferDecision(BaseModel):
    action: Literal["accept", "modify", "reject"]
    revision: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=4_000)
    selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def validate_selected_candidate(self) -> "TransferDecision":
        if self.selected_candidate_id and self.action == "reject":
            raise ValueError("Rejected decisions cannot select a candidate")
        return self


class PaperAcquisition(BaseModel):
    project_id: str
    paper_id: str
    status: Literal["pending", "downloading", "awaiting_upload", "parsed", "failed"]
    source_url: str | None = None
    document_id: str | None = None
    error: str | None = None
    updated_at: str
