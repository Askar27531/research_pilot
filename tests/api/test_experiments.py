from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.api.dependencies import (
    get_checkpointer,
    get_database,
    get_evidence_repository,
    get_llm_provider,
)
from app.db import Database, ProjectRepository
from app.llm import LLMProvider
from app.main import app
from app.schemas import (
    Ablation,
    EvaluationPlan,
    EvidenceNode,
    Experiment,
    ExperimentProposalDraft,
    Hypothesis,
    ResearchRequest,
)


def evidence_node(project_id: str, evidence_id: str) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        project_id=project_id,
        paper_id="paper-1",
        document_id="document-1",
        evidence_type="text",
        claim="Prior work supports registration adapters",
        confidence=0.95,
        page_number=1,
        excerpt="Prior work supports registration adapters",
        span_start=0,
        span_end=41,
        source_path="page.png",
        source_hash="a" * 64,
        created_at="2026-08-09T00:00:00+00:00",
    )


class FakeEvidenceRepository:
    async def get(self, project_id, evidence_id):
        return evidence_node(project_id, evidence_id)


class ProposalProvider(LLMProvider):
    async def chat(self, messages):
        return "unused"

    async def structured_output(self, messages, response_model):
        assert response_model is ExperimentProposalDraft
        assert "experiment-design" in messages[0]["content"]
        return ExperimentProposalDraft(
            hypotheses=[
                Hypothesis(
                    hypothesis_id="h1",
                    statement="Adapters improve cross-spectral registration",
                    evidence_ids=["e1"],
                    confidence=0.8,
                )
            ],
            experiments=[
                Experiment(
                    experiment_id="x1",
                    title="Adapter ablation",
                    baseline="Frozen backbone",
                    modification="Add spectral adapter",
                    controls=["Same data split"],
                    metrics=["registration error"],
                    success_criterion="Error decreases by at least 5%",
                    failure_criterion="Error does not decrease by 5%",
                    evidence_ids=["e1"],
                )
            ],
            ablations=[
                Ablation(
                    ablation_id="a1",
                    experiment_id="x1",
                    removed_component="spectral adapter",
                    fixed_variables=["backbone", "data split"],
                    expected_observation="Registration error increases",
                )
            ],
            evaluation=EvaluationPlan(
                metrics=["registration error"], reporting=["mean and standard deviation"]
            ),
        )

    async def close(self):
        return None


def test_accept_modify_reject_resume_paths_and_version_guard(tmp_path) -> None:
    database = Database(tmp_path / "experiments.db")
    checkpointer = InMemorySaver()

    async def provider_dependency() -> AsyncIterator[LLMProvider]:
        yield ProposalProvider()

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    app.dependency_overrides[get_evidence_repository] = lambda: FakeEvidenceRepository()
    app.dependency_overrides[get_llm_provider] = provider_dependency
    try:
        with TestClient(app) as client:
            client.portal.call(database.initialize)
            results = {}
            for action in ("accept", "modify", "reject"):
                project = client.portal.call(
                    ProjectRepository(database).create,
                    f"Proposal {action}",
                    ResearchRequest(research_question="Design an adapter experiment"),
                )
                created = client.post(
                    f"/projects/{project.id}/experiment-proposal",
                    json={"objective": "Test spectral adapters", "evidence_ids": ["e1"]},
                )
                assert created.status_code == 201
                assert created.json()["interrupted"] is True
                waiting = client.get(f"/projects/{project.id}").json()
                assert waiting["status"] == "waiting"
                body = {"action": action, "version": 1, "feedback": f"{action} feedback"}
                if action == "modify":
                    body["experiment_updates"] = [
                        {
                            "experiment_id": "x1",
                            "metrics": ["median registration error"],
                            "failure_criterion": "Median error fails to decrease by 5%",
                        }
                    ]
                decided = client.post(
                    f"/projects/{project.id}/experiment-proposal/decision", json=body
                )
                assert decided.status_code == 200
                proposal = decided.json()["proposal"]
                assert proposal["status"] == f"{action}ed" if action != "modify" else "modified"
                assert proposal["version"] == 2
                results[action] = proposal
                stale = client.post(
                    f"/projects/{project.id}/experiment-proposal/decision",
                    json={"action": "accept", "version": 1},
                )
                assert stale.status_code == 409
    finally:
        app.dependency_overrides.clear()

    assert results["modify"]["experiments"][0]["metrics"] == ["median registration error"]
