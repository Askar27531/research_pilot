from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.graph import build_research_graph, create_research_state
from app.llm import LLMError, LLMProvider
from app.schemas import ResearchRequest, ResearchUnderstanding


class UnderstandingProvider(LLMProvider):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        if self.fail:
            raise LLMError("model failed")
        assert response_model is ResearchUnderstanding
        return ResearchUnderstanding(
            normalized_goal="Study RGB-LWIR registration",
            core_concepts=["RGB-LWIR", "image registration"],
            domain="computer vision",
            year_from=2000,
            year_to=2001,
            ambiguities=[],
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_graph_understands_request_and_preserves_explicit_years() -> None:
    request = ResearchRequest(
        research_question="RGB-LWIR image registration",
        year_from=2024,
        year_to=2026,
    )
    state = create_research_state(request, project_id="graph-test")
    checkpointer = InMemorySaver()
    graph = build_research_graph(UnderstandingProvider(), checkpointer)
    config: dict[str, Any] = {"configurable": {"thread_id": "graph-test"}}

    result = await graph.ainvoke(state, config=config)

    assert result["current_stage"] == "request_understood"
    assert result["understanding"]["year_from"] == 2024
    assert result["understanding"]["year_to"] == 2026
    checkpoint = await checkpointer.aget_tuple(config)
    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["current_stage"] == "request_understood"


@pytest.mark.asyncio
async def test_graph_allows_missing_years() -> None:
    request = ResearchRequest(research_question="RGB-LWIR registration")
    graph = build_research_graph(UnderstandingProvider())
    result = await graph.ainvoke(create_research_state(request))
    assert result["understanding"]["year_from"] == 2000


@pytest.mark.asyncio
async def test_graph_propagates_provider_error() -> None:
    request = ResearchRequest(research_question="RGB-LWIR registration")
    graph = build_research_graph(UnderstandingProvider(fail=True))
    with pytest.raises(LLMError, match="model failed"):
        await graph.ainvoke(create_research_state(request))


@pytest.mark.asyncio
async def test_graph_removes_garbled_ambiguity_for_chinese_request() -> None:
    class GarbledProvider(UnderstandingProvider):
        async def structured_output(self, messages, response_model):
            result = await super().structured_output(messages, response_model)
            result.ambiguities = ["?? RGB-LWIR registration", "missing dataset"]
            return result

    request = ResearchRequest(
        research_question="调研2024至2026年的RGB-LWIR图像配准方法",
        year_from=2024,
        year_to=2026,
    )
    graph = build_research_graph(GarbledProvider())
    result = await graph.ainvoke(create_research_state(request))
    assert result["understanding"]["ambiguities"] == []
