from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastmcp import Context, FastMCP

from app.literature import OpenAlexClient
from app.schemas import PaperMetadata, SearchResult


def create_literature_server(
    client: OpenAlexClient | None = None,
    client_factory: Callable[[], OpenAlexClient] = OpenAlexClient,
) -> FastMCP:
    """Create a server whose owned OpenAlex client follows server lifespan."""

    @asynccontextmanager
    async def server_lifespan(_: FastMCP) -> AsyncIterator[dict[str, OpenAlexClient]]:
        openalex = client or client_factory()
        try:
            yield {"openalex": openalex}
        finally:
            if client is None:
                await openalex.close()

    server = FastMCP(
        "ResearchPilot Literature",
        instructions="Search and retrieve normalized scholarly paper metadata.",
        lifespan=server_lifespan,
    )

    @server.tool
    async def search_papers(
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
        trace_id: str | None = None,
        ctx: Context | None = None,
    ) -> SearchResult:
        """Search OpenAlex for scholarly papers within an optional year range."""

        if ctx is None:
            raise RuntimeError("FastMCP context is unavailable")
        openalex = ctx.lifespan_context["openalex"]
        return await openalex.search_papers(
            query, year_from, year_to, limit, trace_id=trace_id
        )

    @server.tool
    async def get_paper_metadata(
        identifier: str, ctx: Context | None = None
    ) -> PaperMetadata:
        """Retrieve one normalized paper by OpenAlex ID or DOI."""

        if ctx is None:
            raise RuntimeError("FastMCP context is unavailable")
        openalex = ctx.lifespan_context["openalex"]
        return await openalex.get_paper_metadata(identifier)

    return server


mcp = create_literature_server()


if __name__ == "__main__":
    mcp.run()
