import json

import httpx
import pytest

from app.core.config import Settings
from app.literature.deduplication import deduplicate_papers
from app.literature.errors import (
    LiteratureUnavailableError,
    OpenAlexAuthRequiredError,
    PaperNotFoundError,
)
from app.literature.filtering import filter_by_required_concepts
from app.literature.openalex import OpenAlexClient
from app.literature.ranking import lexical_score, rank_papers
from app.llm import LLMError, LLMProvider
from app.schemas import (
    PaperMetadata,
    PaperScreeningBatch,
    PaperScreeningDecision,
    ResearchRequest,
)


def settings(**overrides) -> Settings:
    return Settings(
        ollama_model="qwen3:14b",
        openalex_base_url="https://openalex.test",
        openalex_api_key=overrides.pop("openalex_api_key", "test-key"),
        openalex_max_attempts=overrides.pop("openalex_max_attempts", 2),
        openalex_backoff_seconds=0,
        **overrides,
    )


def work(identifier: str = "W1", doi: str | None = "https://doi.org/10.1/test") -> dict:
    return {
        "id": f"https://openalex.org/{identifier}",
        "display_name": "RGB Thermal Image Registration",
        "publication_year": 2025,
        "doi": doi,
        "authorships": [
            {"author": {"display_name": "Ada Researcher", "orcid": "https://orcid.org/1"}}
        ],
        "abstract_inverted_index": {"Thermal": [1], "RGB": [0], "registration": [2]},
        "cited_by_count": 12,
        "primary_location": {"source": {"display_name": "Vision Journal"}},
        "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
    }


@pytest.mark.asyncio
async def test_openalex_search_maps_filters_and_work() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == "RGB thermal registration"
        assert "from_publication_date:2024-01-01" in request.url.params["filter"]
        assert request.url.params["api_key"] == "secret"
        return httpx.Response(200, json={"meta": {"count": 1}, "results": [work()]})

    client = httpx.AsyncClient(
        base_url="https://openalex.test", transport=httpx.MockTransport(respond)
    )
    openalex = OpenAlexClient(settings(openalex_api_key="secret"), client)
    result = await openalex.search_papers("RGB thermal registration", 2024, 2026, 10)

    assert result.total_available == 1
    assert result.papers[0].abstract == "RGB Thermal registration"
    assert result.papers[0].doi == "10.1/test"
    assert result.papers[0].venue == "Vision Journal"
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_handles_missing_optional_fields() -> None:
    payload = work(doi=None)
    payload.update({"authorships": None, "abstract_inverted_index": None})

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meta": {}, "results": [payload]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    openalex = OpenAlexClient(settings(), client)
    result = await openalex.search_papers("valid query")
    assert result.papers[0].authors == []
    assert result.papers[0].abstract is None
    assert result.papers[0].stable_id == "W1"
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_retries_429_then_succeeds() -> None:
    attempts = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"meta": {}, "results": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    openalex = OpenAlexClient(settings(), client)
    await openalex.search_papers("valid query")
    assert attempts == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_repeated_503_is_unavailable() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    openalex = OpenAlexClient(settings(), client)
    with pytest.raises(LiteratureUnavailableError, match="503"):
        await openalex.search_papers("valid query")
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_metadata_404() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    openalex = OpenAlexClient(settings(openalex_max_attempts=1), client)
    with pytest.raises(PaperNotFoundError):
        await openalex.get_paper_metadata("W404")
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_fails_fast_without_api_key() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    openalex = OpenAlexClient(settings(openalex_api_key=None), client)
    with pytest.raises(OpenAlexAuthRequiredError, match="OPENALEX_API_KEY"):
        await openalex.search_papers("valid query")
    await client.aclose()


@pytest.mark.asyncio
async def test_openalex_maps_rejected_api_key() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="invalid api key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    openalex = OpenAlexClient(settings(openalex_api_key="invalid"), client)
    with pytest.raises(OpenAlexAuthRequiredError, match="rejected"):
        await openalex.search_papers("valid query")
    await client.aclose()


def test_deduplication_merges_normalized_doi_and_fields() -> None:
    first = PaperMetadata(
        stable_id="one",
        source_id="W1",
        title="Paper",
        doi="https://doi.org/10.1000/ABC",
        citation_count=1,
        source_queries=["q1"],
    )
    second = PaperMetadata(
        stable_id="two",
        source_id="W2",
        title="Paper duplicate",
        doi="doi:10.1000/abc.",
        abstract="A more complete abstract",
        citation_count=5,
        source_queries=["q2"],
    )
    result = deduplicate_papers([first, second])
    assert len(result.unique_papers) == 1
    assert result.unique_papers[0].citation_count == 5
    assert result.unique_papers[0].source_queries == ["q1", "q2"]
    assert len(result.duplicate_groups) == 1


def test_required_concept_filter_removes_adjacent_topics() -> None:
    direct = PaperMetadata(
        stable_id="direct",
        source_id="W1",
        title="Visible and LWIR Image Registration",
        abstract="A cross-modal alignment method",
    )
    fusion = PaperMetadata(
        stable_id="fusion",
        source_id="W2",
        title="Visible and LWIR Image Fusion",
        abstract="Object detection in thermal imagery",
    )
    result = filter_by_required_concepts(
        [direct, fusion],
        [["RGB", "visible"], ["LWIR", "thermal"], ["registration", "alignment"]],
        ["object detection"],
    )
    assert [paper.stable_id for paper in result.included] == ["direct"]
    assert [paper.stable_id for paper in result.excluded] == ["fusion"]


def test_required_concept_filter_does_not_match_short_term_inside_word() -> None:
    paper = PaperMetadata(
        stable_id="false-positive",
        source_id="W3",
        title="First results for visible image registration",
    )
    result = filter_by_required_concepts(
        [paper],
        [["visible"], ["IR"], ["registration"]],
        [],
    )
    assert result.included == []


class FailingRankProvider(LLMProvider):
    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        raise LLMError("ranking unavailable")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ranker_falls_back_to_stable_lexical_order() -> None:
    request = ResearchRequest(
        research_question="RGB thermal image registration", keywords=["thermal", "registration"]
    )
    relevant = PaperMetadata(
        stable_id="relevant", source_id="W1", title="RGB Thermal Image Registration"
    )
    irrelevant = PaperMetadata(
        stable_id="irrelevant", source_id="W2", title="Marine Biology Survey"
    )
    assert lexical_score(relevant, request) > lexical_score(irrelevant, request)
    ranked, warnings = await rank_papers([irrelevant, relevant], request, FailingRankProvider())
    assert ranked[0].paper.stable_id == "relevant"
    assert ranked[0].llm_score is None
    assert warnings


class RecordingScreenProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.paper_payload: list[dict] = []
        self.prompt = ""

    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        assert response_model is PaperScreeningBatch
        self.calls += 1
        self.prompt = messages[-1]["content"]
        self.paper_payload = json.loads(self.prompt.split("\nPapers: ", 1)[1])
        return PaperScreeningBatch(
            decisions=[
                PaperScreeningDecision(
                    stable_id=item["stable_id"],
                    include="registration" in item["title"].casefold(),
                    relevance=90 if "registration" in item["title"].casefold() else 10,
                    reason="Direct task"
                    if "registration" in item["title"].casefold()
                    else "Adjacent task",
                    matched_required_concepts=["registration"],
                )
                for item in self.paper_payload
            ]
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ranker_screens_only_top_ten_once_with_short_snippets() -> None:
    request = ResearchRequest(
        research_question="RGB LWIR image registration",
        keywords=["RGB", "LWIR", "registration"],
    )
    papers = [
        PaperMetadata(
            stable_id=f"W{index:02}",
            source_id=f"W{index:02}",
            title=(
                f"RGB LWIR image registration method {index}"
                if index < 6
                else f"RGB LWIR image fusion method {index}"
            ),
            abstract=("thermal visible alignment evidence " * 100),
        )
        for index in range(15)
    ]
    provider = RecordingScreenProvider()
    ranked, warnings = await rank_papers(
        papers,
        request,
        provider,
        settings=settings(ranking_llm_top_n=10, ranking_abstract_chars=600),
        search_strategy={
            "required_concept_groups": [["RGB"], ["LWIR"], ["registration", "alignment"]],
            "excluded_topics": ["fusion"],
        },
    )

    assert provider.calls == 1
    assert len(provider.paper_payload) == 10
    assert all(len(item["evidence_snippet"]) <= 600 for item in provider.paper_payload)
    assert "Validated search strategy" in provider.prompt
    assert warnings == []
    assert all("registration" in item.paper.title.casefold() for item in ranked[:6])
