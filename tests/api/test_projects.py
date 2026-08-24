from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.api.dependencies import (
    get_checkpointer,
    get_database,
    get_literature_client,
    get_llm_provider,
)
from app.db import Database
from app.literature import LiteratureMCPClient
from app.llm import LLMProvider
from app.main import app
from app.schemas import (
    PaperMetadata,
    PaperScreeningBatch,
    PaperScreeningDecision,
    ResearchUnderstanding,
    SearchQuery,
    SearchQueryPlan,
    SearchResult,
)
from mcp_servers.literature.server import create_literature_server


class ProjectProvider(LLMProvider):
    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        if response_model is ResearchUnderstanding:
            return ResearchUnderstanding(
                normalized_goal="Study RGB-LWIR registration",
                core_concepts=["RGB-LWIR", "registration"],
                domain="computer vision",
            )
        if response_model is SearchQueryPlan:
            assert "systematic-search" in messages[0]["content"]
            return SearchQueryPlan(
                topic="RGB-LWIR registration",
                required_concept_groups=[["RGB"], ["LWIR"], ["registration"]],
                queries=[
                    SearchQuery(query=f"query {index}", purpose=purpose, concepts=["RGB"])
                    for index, purpose in enumerate(("core", "synonym", "method"), start=1)
                ],
            )
        if response_model is PaperScreeningBatch:
            assert "paper-screening" in messages[0]["content"]
            return PaperScreeningBatch(
                decisions=[
                    PaperScreeningDecision(
                        stable_id="10.1/relevant",
                        include=True,
                        relevance=95,
                        reason="Direct match",
                        matched_required_concepts=["RGB", "LWIR", "registration"],
                    )
                ]
            )
        raise AssertionError(f"Unexpected response model: {response_model}")

    async def close(self) -> None:
        return None


class ProjectLiterature:
    def __init__(self) -> None:
        self.trace_ids = []

    async def search_papers(self, query, year_from=None, year_to=None, limit=20, trace_id=None):
        self.trace_ids.append(trace_id)
        return SearchResult(
            query=query,
            papers=[
                PaperMetadata(
                    stable_id="10.1/relevant",
                    source_id="W1",
                    title="RGB LWIR Registration",
                    abstract="RGB LWIR registration",
                    doi="10.1/relevant",
                    year=2025,
                    source_queries=[query],
                )
            ],
            total_available=1,
            source_latency_ms=1,
        )


def test_project_api_persists_results_and_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "api.db")
    literature_backend = ProjectLiterature()
    literature_adapter = LiteratureMCPClient(create_literature_server(literature_backend))

    async def provider_dependency() -> AsyncIterator[LLMProvider]:
        yield ProjectProvider()

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_checkpointer] = lambda: InMemorySaver()
    app.dependency_overrides[get_llm_provider] = provider_dependency
    app.dependency_overrides[get_literature_client] = lambda: literature_adapter
    try:
        with TestClient(app) as client:
            client.portal.call(database.initialize)
            created = client.post(
                "/projects",
                json={
                    "name": "Registration review",
                    "request": {
                        "research_question": "RGB-LWIR image registration",
                        "maximum_papers": 2,
                    },
                },
            )
            assert created.status_code == 201
            project_id = created.json()["id"]

            run = client.post(
                f"/projects/{project_id}/research",
                json={"run_id": "run-1"},
                headers={"X-Request-ID": "trace-api-123"},
            )
            duplicate = client.post(f"/projects/{project_id}/research", json={"run_id": "run-1"})
            project = client.get(f"/projects/{project_id}")
            papers = client.get(f"/projects/{project_id}/papers")
            trace = client.get(f"/projects/{project_id}/trace")
            trace_metrics = client.get(f"/projects/{project_id}/trace/metrics")
            tool_events = client.get(
                f"/projects/{project_id}/trace", params={"event_type": "tool_call"}
            )
            progress = client.get(f"/projects/{project_id}/progress")
            progress_metrics = client.get(f"/projects/{project_id}/progress/metrics")
            missing = client.get("/projects/missing")
    finally:
        app.dependency_overrides.clear()

    assert run.status_code == 200
    assert duplicate.status_code == 200
    assert run.json()["status"] == "completed"
    assert duplicate.json()["selected_paper_count"] == 1
    assert project.json()["current_stage"] == "papers_selected"
    assert len(papers.json()) == 1
    events = trace.json()
    assert all(event["trace_id"] == "trace-api-123" for event in events)
    assert literature_backend.trace_ids == ["trace-api-123"] * 3
    assert trace_metrics.json()["total_events"] == len(events)
    assert trace_metrics.json()["success_rate"] == 1
    assert len(tool_events.json()) == 3
    assert len(progress.json()) == 3
    assert progress_metrics.json()["completed_items"] == 3
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "research_started",
        "agent_plan",
        "agent_handoff",
        "skill_load",
        "tool_call",
        "tool_call",
        "tool_call",
        "skill_load",
        "agent_return",
        "research_completed",
    ]
    assert [
        event["summary"]["name"] for event in events if event["event_type"] == "skill_load"
    ] == [
        "systematic-search",
        "paper-screening",
    ]
    handoff = next(event for event in events if event["event_type"] == "agent_handoff")
    assert handoff["summary"]["source_agent"] == "coordinator"
    assert handoff["summary"]["target_agent"] == "literature_researcher"
    assert "request" not in handoff["summary"]["context_summary"]
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
