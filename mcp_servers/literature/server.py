from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastmcp import Context, FastMCP

from app.literature import (
    ArxivProvider,
    CrossrefProvider,
    MultiSourceLiteratureProvider,
    OpenAlexClient,
)
from app.schemas import PaperMetadata, SearchResult


def create_literature_server(
    client: object | None = None,
    client_factory: Callable[[], object] | None = None,
    auth: object | None = None,
) -> FastMCP:
    """Create a server whose owned OpenAlex client follows server lifespan."""

    @asynccontextmanager
    async def server_lifespan(_: FastMCP) -> AsyncIterator[dict[str, object]]:
        literature = client or (client_factory() if client_factory else MultiSourceLiteratureProvider({
            "openalex": OpenAlexClient(), "crossref": CrossrefProvider(), "arxiv": ArxivProvider(),
        }))
        try:
            yield {"literature": literature}
        finally:
            if client is None:
                await literature.close()  # type: ignore[attr-defined]

    server = FastMCP(
        "ResearchPilot Literature",
        instructions="Search and retrieve normalized scholarly paper metadata.",
        lifespan=server_lifespan,
        auth=auth,
        version="0.1.0",
    )

    @server.tool
    async def search_papers(
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
        trace_id: str | None = None,
        sources: list[str] | None = None,
        ctx: Context | None = None,
    ) -> SearchResult:
        """Search OpenAlex for scholarly papers within an optional year range."""

        if ctx is None:
            raise RuntimeError("FastMCP context is unavailable")
        literature = ctx.lifespan_context["literature"]
        if isinstance(literature, MultiSourceLiteratureProvider):
            return await literature.search_papers(query, year_from, year_to, limit, sources)
        return await literature.search_papers(  # type: ignore[attr-defined]
            query, year_from, year_to, limit, trace_id=trace_id)

    @server.tool
    async def get_paper_metadata(
        identifier: str, ctx: Context | None = None
    ) -> PaperMetadata:
        """Retrieve one normalized paper by OpenAlex ID or DOI."""

        if ctx is None:
            raise RuntimeError("FastMCP context is unavailable")
        literature = ctx.lifespan_context["literature"]
        if isinstance(literature, MultiSourceLiteratureProvider):
            # DOI lookup remains authoritative through OpenAlex; search falls back to all sources.
            openalex = literature.providers["openalex"]
            return await openalex.get_paper_metadata(identifier)  # type: ignore[attr-defined]
        return await literature.get_paper_metadata(identifier)  # type: ignore[attr-defined]

    return server


mcp = create_literature_server()


if __name__ == "__main__":
    mcp.run()
