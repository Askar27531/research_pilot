from typing import Any, Protocol

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.literature.errors import LiteratureUnavailableError, OpenAlexAuthRequiredError
from app.schemas import PaperMetadata, SearchResult


class LiteratureToolClient(Protocol):
    async def search_papers(
        self, query: str, year_from: int | None = None, year_to: int | None = None,
        limit: int = 20, trace_id: str | None = None,
        sources: list[str] | None = None,
    ) -> SearchResult: ...

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata: ...


class LiteratureMCPClient:
    """Typed application adapter that always crosses the MCP tool boundary."""

    def __init__(self, server: FastMCP | Any) -> None:
        self.server = server

    async def search_papers(
        self,
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
        trace_id: str | None = None,
        sources: list[str] | None = None,
    ) -> SearchResult:
        try:
            async with Client(self.server) as client:
                result = await client.call_tool(
                    "search_papers",
                    {
                        "query": query,
                        "year_from": year_from,
                        "year_to": year_to,
                        "limit": limit,
                        "trace_id": trace_id,
                        "sources": sources,
                    },
                )
        except ToolError as exc:
            if "OPENALEX_API_KEY" in str(exc) or "OpenAlex rejected" in str(exc):
                raise OpenAlexAuthRequiredError(str(exc)) from exc
            raise LiteratureUnavailableError(f"Literature MCP search failed: {exc}") from exc
        return SearchResult.model_validate(result.structured_content)

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata:
        try:
            async with Client(self.server) as client:
                result = await client.call_tool("get_paper_metadata", {"identifier": identifier})
        except ToolError as exc:
            if "OPENALEX_API_KEY" in str(exc) or "OpenAlex rejected" in str(exc):
                raise OpenAlexAuthRequiredError(str(exc)) from exc
            raise LiteratureUnavailableError(
                f"Literature MCP metadata lookup failed: {exc}"
            ) from exc
        return PaperMetadata.model_validate(result.structured_content)
