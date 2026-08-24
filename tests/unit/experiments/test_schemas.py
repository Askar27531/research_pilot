import pytest
from pydantic import ValidationError

from app.schemas import EvaluationPlan, Experiment, ExperimentProposalDraft, Hypothesis


def experiment(experiment_id: str = "x1") -> Experiment:
    return Experiment(
        experiment_id=experiment_id,
        title="Adapter test",
        baseline="No adapter",
        modification="Add adapter",
        controls=["same data"],
        metrics=["error"],
        success_criterion="Error decreases",
        failure_criterion="Error does not decrease",
        evidence_ids=["e1"],
    )


def test_experiment_requires_failure_criterion_and_evidence() -> None:
    data = experiment().model_dump()
    data["failure_criterion"] = ""
    with pytest.raises(ValidationError):
        Experiment.model_validate(data)
    data = experiment().model_dump()
    data["evidence_ids"] = []
    with pytest.raises(ValidationError):
        Experiment.model_validate(data)


def test_duplicate_experiments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExperimentProposalDraft(
            hypotheses=[
                Hypothesis(
                    hypothesis_id="h1",
                    statement="Adapters improve registration",
                    evidence_ids=["e1"],
                    confidence=0.8,
                )
            ],
            experiments=[experiment("x1"), experiment("x2")],
            evaluation=EvaluationPlan(metrics=["error"], reporting=["mean"]),
        )
