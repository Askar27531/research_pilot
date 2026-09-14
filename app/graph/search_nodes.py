from collections.abc import Awaitable, Callable

from app.graph.state import ResearchState
from app.literature.deduplication import deduplicate_papers
from app.literature.errors import (
    LiteratureError,
    LiteratureUnavailableError,
    OpenAlexAuthRequiredError,
)
from app.literature.filtering import filter_by_required_concepts
from app.literature.mcp_client import LiteratureToolClient
from app.literature.query import generate_search_queries
from app.literature.ranking import rank_papers
from app.llm import LLMProvider
from app.schemas import (
    PaperMetadata,
    RankedPaper,
    ResearchRequest,
    ResearchUnderstanding,
    SearchQuery,
)

SearchNode = Callable[[ResearchState], Awaitable[dict[str, object]]]


def make_generate_queries_node(provider: LLMProvider) -> SearchNode:
    async def generate_queries(state: ResearchState) -> dict[str, object]:
        request = ResearchRequest.model_validate(state["request"])
        understanding = ResearchUnderstanding.model_validate(state["understanding"])
        plan, warnings = await generate_search_queries(request, understanding, provider)
        graph_plan = dict(state["plan"])
        graph_plan["search_strategy"] = {
            "topic": plan.topic,
            "required_concept_groups": plan.required_concept_groups,
            "excluded_topics": plan.excluded_topics,
        }
        return {
            "search_queries": [query.model_dump(mode="json") for query in plan.queries],
            "plan": graph_plan,
            "warnings": [*state["warnings"], *warnings],
            "current_stage": "queries_generated",
        }

    return generate_queries


def make_search_papers_node(literature: LiteratureToolClient) -> SearchNode:
    async def search(state: ResearchState) -> dict[str, object]:
        request = ResearchRequest.model_validate(state["request"])
        queries = [SearchQuery.model_validate(item) for item in state["search_queries"]]
        papers: list[PaperMetadata] = []
        warnings = list(state["warnings"])
        successes = 0
        per_query_limit = min(30, max(request.maximum_papers * 2, 10))
        for query in queries:
            try:
                result = await literature.search_papers(
                    query.query, request.year_from, request.year_to, per_query_limit,
                    sources=request.literature_sources,
                )
                papers.extend(result.papers)
                warnings.extend(result.warnings)
                successes += 1
            except OpenAlexAuthRequiredError:
                raise
            except LiteratureError as exc:
                warnings.append(f"Search failed for '{query.query}': {exc}")
        if successes == 0:
            raise LiteratureUnavailableError("All literature queries failed")
        return {
            "candidate_papers": [paper.model_dump(mode="json") for paper in papers],
            "warnings": warnings,
            "current_stage": "papers_searched",
        }

    return search


async def deduplicate_papers_node(state: ResearchState) -> dict[str, object]:
    papers = [PaperMetadata.model_validate(item) for item in state["candidate_papers"]]
    result = deduplicate_papers(papers)
    plan = dict(state["plan"])
    plan["deduplication"] = {
        "input_count": len(papers),
        "unique_count": len(result.unique_papers),
        "duplicate_groups": result.duplicate_groups,
        "merge_log": result.merge_log,
    }
    return {
        "candidate_papers": [paper.model_dump(mode="json") for paper in result.unique_papers],
        "plan": plan,
        "current_stage": "papers_deduplicated",
    }


async def filter_papers_node(state: ResearchState) -> dict[str, object]:
    papers = [PaperMetadata.model_validate(item) for item in state["candidate_papers"]]
    strategy = state["plan"].get("search_strategy", {})
    result = filter_by_required_concepts(
        papers,
        strategy.get("required_concept_groups", []),
        strategy.get("excluded_topics", []),
    )
    fallback_used = not result.included
    included = result.included or papers
    plan = dict(state["plan"])
    plan["concept_filter"] = {
        "input_count": len(papers),
        "included_count": len(included),
        "excluded_count": len(result.excluded),
        "matched_concepts": result.matched_concepts,
        "fallback_used": fallback_used,
    }
    return {
        "candidate_papers": [paper.model_dump(mode="json") for paper in included],
        "plan": plan,
        "warnings": [
            *state["warnings"],
            *(["Concept filter matched no candidates; deferred inclusion to LLM screening"]
              if fallback_used else []),
        ],
        "current_stage": "papers_concept_filtered",
    }


def make_enrich_arxiv_abstracts_node(literature) -> SearchNode:
    async def enrich(state: ResearchState) -> dict[str, object]:
        enrich_abstracts = getattr(literature, "get_arxiv_abstract", None)
        if enrich_abstracts is None:
            return {}
        papers = [PaperMetadata.model_validate(item) for item in state["candidate_papers"]]
        missing = [
            paper for paper in papers
            if paper.arxiv_id and not (paper.abstract or "").strip()
        ][:10]
        if not missing:
            return {}
        warnings = list(state["warnings"])
        by_id = {paper.arxiv_id: paper for paper in papers}
        changed = False
        for arxiv_id in [paper.arxiv_id for paper in missing]:
            paper = by_id[arxiv_id]
            try:
                meta = await enrich_abstracts(arxiv_id)
            except Exception as exc:  # noqa: BLE001 - enrichment is best effort
                warnings.append(f"arXiv abstract enrichment failed for {arxiv_id}: {exc}")
                continue
            if meta is None:
                continue
            updates = {}
            if not (paper.abstract or "").strip() and (meta.abstract or "").strip():
                updates["abstract"] = meta.abstract
            if paper.year is None and meta.year is not None:
                updates["year"] = meta.year
            if updates:
                papers[papers.index(paper)] = paper.model_copy(update=updates)
                changed = True
        if not changed:
            return {"warnings": warnings}
        return {
            "candidate_papers": [value.model_dump(mode="json") for value in papers],
            "warnings": warnings,
            "current_stage": "papers_abstracts_enriched",
        }

    return enrich


def make_rank_papers_node(provider: LLMProvider) -> SearchNode:
    async def rank(state: ResearchState) -> dict[str, object]:
        request = ResearchRequest.model_validate(state["request"])
        papers = [PaperMetadata.model_validate(item) for item in state["candidate_papers"]]
        ranked, warnings = await rank_papers(
            papers,
            request,
            provider,
            search_strategy=state["plan"].get("search_strategy", {}),
        )
        return {
            "ranked_papers": [paper.model_dump(mode="json") for paper in ranked],
            "warnings": [*state["warnings"], *warnings],
            "current_stage": "papers_ranked",
        }

    return rank


async def select_papers_node(state: ResearchState) -> dict[str, object]:
    request = ResearchRequest.model_validate(state["request"])
    ranked = [RankedPaper.model_validate(item) for item in state["ranked_papers"]]
    selected = ranked[: request.maximum_papers]
    return {
        "selected_papers": [item.model_dump(mode="json") for item in selected],
        "current_stage": "papers_selected",
    }
