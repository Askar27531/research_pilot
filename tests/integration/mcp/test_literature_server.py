from fastmcp import Client

from app.literature.errors import OpenAlexAuthRequiredError
from app.literature.mcp_client import LiteratureMCPClient
from app.schemas import PaperMetadata, SearchResult
from mcp_servers.literature.server import create_literature_server


class FakeOpenAlex:
    def __init__(self) -> None:
        self.closed = False
        self.trace_ids = []

    async def search_papers(self, query, year_from=None, year_to=None, limit=20, trace_id=None):
        self.trace_ids.append(trace_id)
        return SearchResult(
            query=query,
            papers=[PaperMetadata(stable_id="W1", source_id="W1", title="Test Paper")],
            total_available=1,
            source_latency_ms=1,
        )

    async def get_paper_metadata(self, identifier):
        return PaperMetadata(stable_id=identifier, source_id=identifier, title="Test Paper")

    async def close(self) -> None:
        self.closed = True


async def test_mcp_exposes_and_calls_literature_tools() -> None:
    server = create_literature_server(FakeOpenAlex())
    async with Client(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool("search_papers", {"query": "test query"})
    assert {tool.name for tool in tools} == {"search_papers", "get_paper_metadata"}
    assert result.structured_content["papers"][0]["title"] == "Test Paper"


async def test_typed_mcp_adapter_round_trip() -> None:
    openalex = FakeOpenAlex()
    adapter = LiteratureMCPClient(create_literature_server(openalex))
    result = await adapter.search_papers("test query", 2024, 2026, 10, trace_id="trace-123")
    paper = await adapter.get_paper_metadata("W1")
    assert result.total_available == 1
    assert paper.source_id == "W1"
    assert openalex.trace_ids == ["trace-123"]


async def test_server_lifespan_closes_owned_client_only() -> None:
    owned_clients: list[FakeOpenAlex] = []

    def factory():
        client = FakeOpenAlex()
        owned_clients.append(client)
        return client

    adapter = LiteratureMCPClient(create_literature_server(client_factory=factory))
    await adapter.search_papers("test query")
    assert len(owned_clients) == 1
    assert owned_clients[0].closed is True

    external = FakeOpenAlex()
    external_adapter = LiteratureMCPClient(create_literature_server(external))
    await external_adapter.search_papers("test query")
    assert external.closed is False


async def test_mcp_adapter_preserves_openalex_auth_error() -> None:
    class MissingKeyOpenAlex(FakeOpenAlex):
        async def search_papers(self, query, year_from=None, year_to=None, limit=20, trace_id=None):
            raise OpenAlexAuthRequiredError("OPENALEX_API_KEY is required")

    adapter = LiteratureMCPClient(create_literature_server(MissingKeyOpenAlex()))
    try:
        await adapter.search_papers("test query")
    except OpenAlexAuthRequiredError as exc:
        assert "OPENALEX_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected OpenAlexAuthRequiredError")
