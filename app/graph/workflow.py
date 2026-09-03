from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import make_understand_request_node
from app.graph.search_nodes import (
    deduplicate_papers_node,
    filter_papers_node,
    make_enrich_arxiv_abstracts_node,
    make_generate_queries_node,
    make_rank_papers_node,
    make_search_papers_node,
    select_papers_node,
)
from app.graph.state import ResearchState
from app.literature import LiteratureToolClient
from app.llm import LLMProvider


def build_literature_search_graph(
    provider: LLMProvider,
    literature: LiteratureToolClient,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the P1 literature search, deduplication, ranking, and selection workflow."""

    builder = StateGraph(ResearchState)
    builder.add_node("understand_request", make_understand_request_node(provider))
    builder.add_node("generate_queries", make_generate_queries_node(provider))
    builder.add_node("search_papers", make_search_papers_node(literature))
    builder.add_node("deduplicate_papers", deduplicate_papers_node)
    builder.add_node("filter_papers", filter_papers_node)
    builder.add_node(
        "enrich_arxiv_abstracts", make_enrich_arxiv_abstracts_node(literature)
    )
    builder.add_node("rank_papers", make_rank_papers_node(provider))
    builder.add_node("select_papers", select_papers_node)
    builder.add_edge(START, "understand_request")
    builder.add_edge("understand_request", "generate_queries")
    builder.add_edge("generate_queries", "search_papers")
    builder.add_edge("search_papers", "deduplicate_papers")
    builder.add_edge("deduplicate_papers", "filter_papers")
    builder.add_edge("filter_papers", "enrich_arxiv_abstracts")
    builder.add_edge("enrich_arxiv_abstracts", "rank_papers")
    builder.add_edge("rank_papers", "select_papers")
    builder.add_edge("select_papers", END)
    return builder.compile(checkpointer=checkpointer)
