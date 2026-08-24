from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.api.dependencies import get_literature_client, get_llm_provider
from app.literature.errors import OpenAlexAuthRequiredError
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


class SearchProvider(LLMProvider):
    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        if response_model is ResearchUnderstanding:
            return ResearchUnderstanding(
                normalized_goal="Study RGB-LWIR registration",
                core_concepts=["RGB-LWIR", "registration"],
                domain="computer vision",
                year_from=2024,
                year_to=2026,
            )
        if response_model is SearchQueryPlan:
            return SearchQueryPlan(
                topic="",
                required_concept_groups=[
                    ["RGB", "visible"],
                    ["LWIR", "thermal", "infrared"],
                    ["registration", "alignment"],
                ],
                excluded_topics=["object detection"],
                queries=[
                    SearchQuery(
                        query="RGB LWIR image registration",
                        purpose="core",
                        concepts=["RGB", "LWIR", "registration"],
                    ),
                    SearchQuery(
                        query="visible thermal cross spectral registration",
                        purpose="synonym",
                        concepts=["visible", "thermal", "registration"],
                    ),
                    SearchQuery(
                        query="RGB infrared registration deep learning",
                        purpose="method",
                        concepts=["RGB", "infrared", "deep learning"],
                    ),
                ],
            )
        if response_model is PaperScreeningBatch:
            return PaperScreeningBatch(
                decisions=[
                    PaperScreeningDecision(
                        stable_id="10.1/relevant",
                        include=True,
                        relevance=95,
                        reason="Directly addresses cross-spectral image registration",
                        matched_required_concepts=["RGB", "LWIR", "registration"],
                    ),
                ]
            )
        raise AssertionError(f"Unexpected response model: {response_model}")

    async def close(self) -> None:
        return None


class SearchLiterature:
    async def search_papers(self, query, year_from=None, year_to=None, limit=20):
        return SearchResult(
            query=query,
            papers=[
                PaperMetadata(
                    stable_id="10.1/relevant",
                    source_id="W1",
                    title="RGB-LWIR Image Registration",
                    abstract="Cross spectral visible thermal registration method",
                    doi="10.1/relevant",
                    year=2025,
                    source_queries=[query],
                ),
                PaperMetadata(
                    stable_id="10.1/other",
                    source_id="W2",
                    title="Unrelated Study",
                    doi="10.1/other",
                    year=2024,
                    source_queries=[query],
                ),
            ],
            total_available=2,
            source_latency_ms=1,
        )


def test_literature_search_endpoint_runs_complete_graph() -> None:
    async def provider_dependency() -> AsyncIterator[LLMProvider]:
        yield SearchProvider()

    app.dependency_overrides[get_llm_provider] = provider_dependency
    app.dependency_overrides[get_literature_client] = lambda: SearchLiterature()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/research/search",
                json={
                    "research_question": "RGB-LWIR image registration",
                    "keywords": ["RGB-LWIR", "registration"],
                    "year_from": 2024,
                    "year_to": 2026,
                    "maximum_papers": 2,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["current_stage"] == "papers_selected"
    assert len(body["queries"]) == 3
    assert body["candidate_count"] == 1
    assert body["deduplication"]["input_count"] == 6
    assert body["deduplication"]["unique_count"] == 2
    assert body["concept_filter"]["included_count"] == 1
    assert body["concept_filter"]["excluded_count"] == 1
    assert body["search_strategy"]["topic"] == "RGB-LWIR image registration"
    assert body["selected_papers"][0]["paper"]["stable_id"] == "10.1/relevant"


def test_literature_search_reports_missing_openalex_key() -> None:
    class MissingAuthLiterature:
        async def search_papers(self, query, year_from=None, year_to=None, limit=20):
            raise OpenAlexAuthRequiredError("OPENALEX_API_KEY is required")

    async def provider_dependency() -> AsyncIterator[LLMProvider]:
        yield SearchProvider()

    app.dependency_overrides[get_llm_provider] = provider_dependency
    app.dependency_overrides[get_literature_client] = lambda: MissingAuthLiterature()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/research/search",
                json={"research_question": "RGB-LWIR image registration"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "OPENALEX_AUTH_REQUIRED"
    assert response.json()["error"]["retryable"] is False
