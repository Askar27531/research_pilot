from app.llm import LLMError, LLMProvider
from app.schemas import ResearchRequest, ResearchUnderstanding, SearchQuery, SearchQueryPlan


async def generate_search_queries(
    request: ResearchRequest,
    understanding: ResearchUnderstanding,
    provider: LLMProvider,
) -> tuple[SearchQueryPlan, list[str]]:
    warnings: list[str] = []
    # 检索策略指令由 SkillAwareProvider 按 SearchQueryPlan 注入 systematic-search 技能，
    # 本函数只提供输入；生成质量由技能约束，数量/覆盖等确定性校验见 _deduplicate_query_plan。
    try:
        plan = await provider.structured_output(
            [
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
    """Keep complementary queries while preferring those covering all groups.

    Concept filtering is recall-first (any matched group keeps a paper), so the
    plan should keep broad/partial-coverage queries as well. Queries that cover
    every required group are kept first; remaining complementary queries fill up
    to five slots. Near-duplicate wording is dropped regardless of coverage.
    """
    unique: list[SearchQuery] = []
    seen: set[str] = set()
    groups = _normalize_groups(plan.required_concept_groups)
    if not groups:
        raise ValueError("model did not return required concept groups")

    def searchable(item: SearchQuery) -> str:
        normalized = " ".join(item.query.casefold().split())
        return f"{normalized} {' '.join(item.concepts).casefold()}"

    def covers_all(item: SearchQuery) -> bool:
        return all(
            any(_contains(searchable(item), term.casefold()) for term in group)
            for group in groups
        )

    def covers_any(item: SearchQuery) -> bool:
        return any(
            any(_contains(searchable(item), term.casefold()) for term in group)
            for group in groups
        )

    # Strong (all-groups) queries first, then broader recall-oriented ones.
    ordered = sorted(plan.queries, key=lambda item: not covers_all(item))
    for item in ordered:
        normalized = " ".join(item.query.casefold().split())
        if normalized in seen or not covers_any(item):
            continue
        seen.add(normalized)
        unique.append(item)
        if len(unique) >= 5:
            break
    if len(unique) < 3:
        raise ValueError("fewer than 3 distinct queries match any required concept group")
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
