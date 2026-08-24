import aiosqlite
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.graph import build_research_graph, create_research_state
from app.llm import LLMProvider
from app.schemas import ResearchRequest, ResearchUnderstanding


class Provider(LLMProvider):
    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        assert response_model is ResearchUnderstanding
        return ResearchUnderstanding(
            normalized_goal="Study registration",
            core_concepts=["RGB-LWIR", "registration"],
            domain="computer vision",
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_checkpoint_survives_connection_restart(tmp_path) -> None:
    path = tmp_path / "checkpoints.db"
    config = {"configurable": {"thread_id": "project-1"}}
    serializer = JsonPlusSerializer(allowed_msgpack_modules=[])

    first_connection = await aiosqlite.connect(path)
    first_saver = AsyncSqliteSaver(first_connection, serde=serializer)
    await first_saver.setup()
    first_graph = build_research_graph(Provider(), first_saver)
    result = await first_graph.ainvoke(
        create_research_state(
            ResearchRequest(research_question="RGB-LWIR registration"),
            project_id="project-1",
        ),
        config=config,
    )
    assert result["current_stage"] == "request_understood"
    await first_connection.close()

    second_connection = await aiosqlite.connect(path)
    second_saver = AsyncSqliteSaver(second_connection, serde=serializer)
    second_graph = build_research_graph(Provider(), second_saver)
    snapshot = await second_graph.aget_state(config)
    await second_connection.close()

    assert snapshot.values["project_id"] == "project-1"
    assert snapshot.values["current_stage"] == "request_understood"
    assert snapshot.next == ()
