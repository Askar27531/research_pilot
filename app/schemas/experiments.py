from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Hypothesis(BaseModel):
    hypothesis_id: str
    statement: str = Field(min_length=5, max_length=4_000)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list, max_length=20)


class Experiment(BaseModel):
    experiment_id: str
    title: str = Field(min_length=3, max_length=500)
    baseline: str = Field(min_length=1, max_length=2_000)
    modification: str = Field(min_length=1, max_length=2_000)
    controls: list[str] = Field(min_length=1, max_length=30)
    metrics: list[str] = Field(min_length=1, max_length=30)
    success_criterion: str = Field(min_length=3, max_length=2_000)
    failure_criterion: str = Field(min_length=3, max_length=2_000)
    evidence_ids: list[str] = Field(min_length=1)


class Ablation(BaseModel):
    ablation_id: str
    experiment_id: str
    removed_component: str = Field(min_length=1, max_length=500)
    fixed_variables: list[str] = Field(min_length=1)
    expected_observation: str = Field(min_length=3, max_length=2_000)


class EvaluationPlan(BaseModel):
    metrics: list[str] = Field(min_length=1)
    reporting: list[str] = Field(min_length=1)
    statistical_tests: list[str] = Field(default_factory=list)


class ExperimentProposalDraft(BaseModel):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=10)
    experiments: list[Experiment] = Field(min_length=1, max_length=20)
    ablations: list[Ablation] = Field(default_factory=list, max_length=50)
    evaluation: EvaluationPlan

    @model_validator(mode="after")
    def reject_duplicates(self) -> "ExperimentProposalDraft":
        keys = [
            (item.baseline.strip().casefold(), item.modification.strip().casefold())
            for item in self.experiments
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate baseline/modification experiments are not allowed")
        experiment_ids = {item.experiment_id for item in self.experiments}
        if any(item.experiment_id not in experiment_ids for item in self.ablations):
            raise ValueError("Ablation references an unknown experiment")
        return self


class ExperimentProposal(ExperimentProposalDraft):
    proposal_id: str
    project_id: str
    version: int = Field(ge=1)
    status: Literal["pending", "accepted", "modified", "rejected"]
    created_at: str
    updated_at: str
    decision_feedback: str | None = None


class ProposalCreateRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=4_000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class ExperimentUpdate(BaseModel):
    experiment_id: str
    modification: str | None = Field(default=None, min_length=1, max_length=2_000)
    controls: list[str] | None = Field(default=None, min_length=1, max_length=30)
    metrics: list[str] | None = Field(default=None, min_length=1, max_length=30)
    success_criterion: str | None = Field(default=None, min_length=3, max_length=2_000)
    failure_criterion: str | None = Field(default=None, min_length=3, max_length=2_000)


class ProposalDecision(BaseModel):
    action: Literal["accept", "modify", "reject"]
    version: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=4_000)
    experiment_updates: list[ExperimentUpdate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_action(self) -> "ProposalDecision":
        if self.action == "modify" and not self.experiment_updates:
            raise ValueError("Modify requires at least one experiment update")
        if self.action != "modify" and self.experiment_updates:
            raise ValueError("Experiment updates are only allowed for modify")
        return self


class ProposalRunResponse(BaseModel):
    proposal: ExperimentProposal
    interrupted: bool
