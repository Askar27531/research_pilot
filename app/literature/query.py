from app.llm import LLMError, LLMProvider
from app.schemas import ResearchRequest, ResearchUnderstanding, SearchQuery, SearchQueryPlan


async def generate_search_queries(
    request: ResearchRequest,
    understanding: ResearchUnderstanding,
    provider: LLMProvider,
) -> tuple[SearchQueryPlan, list[str]]:
    warnings: list[str] = []
    try:
        plan = await provider.structured_output(
            [
                {
                    "role": "system",
                    "content": (
                        "Build a high-precision scholarly search strategy from the user's topic. "
                        "Identify 2-5 required concept groups; each group contains synonyms and "
                        "every included paper must match at least one term from every group. Add "
                        "specific excluded topics that are commonly confused with the topic. "
                        "Generate 3-5 English queries and make every query cover every required "
                        "concept group. Do not generate broad application-only or metrics-only queries."
                    ),
                },
                {
                    "role": "user",
                    "content": request.model_dump_json()
                    + "\nUnderstanding: "
                    + understanding.model_dump_json(),
                },
            ],
            SearchQueryPlan,
        )
        plan = _deduplicate_query_plan(plan)
        if not plan.topic.strip():
            plan.topic = request.research_question
        if len(plan.queries) >= 3:
            return plan, warnings
        warnings.append("Model returned fewer than 3 unique queries; used deterministic fallback")
    except (LLMError, ValueError) as exc:
        warnings.append(f"Query generation fallback: {exc}")
    return _fallback_plan(request, understanding), warnings


def _deduplicate_query_plan(plan: SearchQueryPlan) -> SearchQueryPlan:
    unique: list[SearchQuery] = []
    seen: set[str] = set()
    groups = _normalize_groups(plan.required_concept_groups)
    if not groups:
        raise ValueError("model did not return required concept groups")
    for item in plan.queries:
        normalized = " ".join(item.query.casefold().split())
        searchable = f"{normalized} {' '.join(item.concepts).casefold()}"
        covers_all = all(
            any(_contains(searchable, term.casefold()) for term in group) for group in groups
        )
        if normalized not in seen and covers_all:
            seen.add(normalized)
            unique.append(item)
    if len(unique) < 3:
        raise ValueError("fewer than 3 distinct queries cover every required concept group")
    return SearchQueryPlan(
        topic=plan.topic,
        required_concept_groups=groups,
        excluded_topics=list(dict.fromkeys(plan.excluded_topics)),
        queries=unique[:5],
    )


def _fallback_plan(
    request: ResearchRequest, understanding: ResearchUnderstanding
) -> SearchQueryPlan:
    concepts = list(dict.fromkeys([*request.keywords, *understanding.core_concepts]))
    core = " ".join(concepts) or request.research_question
    seeds = request.keywords[:3] or [core]
    while len(seeds) < 3:
        suffix = ("survey", "method algorithm")[len(seeds) - 1]
        seeds.append(f"{seeds[0]} {suffix}")
    purposes = ("high_precision", "synonym_expansion", "method_expansion")
    queries = [
        SearchQuery(query=seed, purpose=purpose, concepts=[seed])
        for seed, purpose in zip(seeds, purposes, strict=True)
    ]
    return SearchQueryPlan(
        topic=request.research_question,
        required_concept_groups=[],
        excluded_topics=[],
        queries=queries,
    )


def _normalize_groups(groups: list[list[str]]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for group in groups:
        terms = list(dict.fromkeys(term.strip() for term in group if term.strip()))
        if terms:
            normalized.append(terms)
    return normalized


def _contains(searchable: str, term: str) -> bool:
    normalized_searchable = " ".join(searchable.replace("-", " ").split())
    normalized_term = " ".join(term.replace("-", " ").split())
    return f" {normalized_term} " in f" {normalized_searchable} "
