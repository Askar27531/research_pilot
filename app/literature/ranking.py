import json
import re
from hashlib import sha256

from app.core.config import Settings, get_settings
from app.llm import LLMError, LLMProvider
from app.schemas import (
    PaperMetadata,
    PaperScreeningBatch,
    RankedPaper,
    ResearchRequest,
)

TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+-]*")
STOPWORDS = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with", "year"}
_screening_cache: dict[str, tuple[bool, float, str, list[str]]] = {}


def tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_PATTERN.findall(value)
        if token.casefold() not in STOPWORDS
    }


def lexical_score(paper: PaperMetadata, request: ResearchRequest) -> float:
    goal_tokens = tokens(" ".join([request.research_question, *request.keywords]))
    if not goal_tokens:
        return 0.0
    title_tokens = tokens(paper.title)
    abstract_tokens = tokens(paper.abstract or "")
    keyword_tokens = tokens(" ".join(request.keywords))
    title_overlap = len(goal_tokens & title_tokens) / len(goal_tokens)
    abstract_overlap = len(goal_tokens & abstract_tokens) / len(goal_tokens)
    keyword_coverage = (
        len(keyword_tokens & (title_tokens | abstract_tokens)) / len(keyword_tokens)
        if keyword_tokens
        else title_overlap
    )
    recency = 0.0
    if paper.year is not None and request.year_from is not None and request.year_to is not None:
        span = max(1, request.year_to - request.year_from)
        recency = min(1.0, max(0.0, (paper.year - request.year_from) / span))
    score = 0.50 * title_overlap + 0.30 * abstract_overlap + 0.15 * keyword_coverage
    score += 0.05 * recency
    return round(min(1.0, max(0.0, score)), 6)


async def rank_papers(
    papers: list[PaperMetadata],
    request: ResearchRequest,
    provider: LLMProvider,
    settings: Settings | None = None,
    search_strategy: dict[str, object] | None = None,
) -> tuple[list[RankedPaper], list[str]]:
    config = settings or get_settings()
    lexical = sorted(
        ((paper, lexical_score(paper, request)) for paper in papers),
        key=lambda item: (-item[1], item[0].stable_id),
    )
    screening_limit = min(config.ranking_llm_top_n, max(request.maximum_papers, 3))
    candidates = lexical[:screening_limit]
    llm_scores: dict[str, tuple[bool, float, str, list[str]]] = {}
    warnings: list[str] = []
    uncached: list[tuple[PaperMetadata, float]] = []
    for paper, score in candidates:
        key = _cache_key(request, paper, config.ollama_model)
        cached = _screening_cache.get(key)
        if cached is None:
            uncached.append((paper, score))
        else:
            llm_scores[paper.stable_id] = cached

    if uncached:
        compact = [
            {
                "stable_id": paper.stable_id,
                "title": paper.title,
                "year": paper.year,
                "evidence_snippet": _relevant_snippet(
                    paper.abstract or "", request, config.ranking_abstract_chars
                ),
            }
            for paper, _ in uncached
        ]
        try:
            result = await provider.structured_output(
                [
                    {
                        "role": "system",
                        "content": (
                            "Screen papers for direct relevance to the research topic. Return every "
                            "stable_id exactly once. Set include=false for adjacent tasks such as "
                            "fusion, detection, sensing, or reviews that do not directly study the "
                            "requested task. Give a 0-100 relevance score and a very short reason."
                        ),
                    },
                    {
                        "role": "user",
                        "content": request.model_dump_json()
                        + "\nValidated search strategy: "
                        + json.dumps(search_strategy or {}, ensure_ascii=False)
                        + "\nPapers: "
                        + json.dumps(compact, ensure_ascii=False),
                    },
                ],
                PaperScreeningBatch,
            )
            allowed = {paper.stable_id for paper, _ in uncached}
            for decision in result.decisions:
                if decision.stable_id in allowed:
                    value = (
                        decision.include,
                        decision.relevance / 100,
                        decision.reason,
                        decision.matched_required_concepts,
                    )
                    llm_scores[decision.stable_id] = value
                    paper = next(
                        item for item, _ in uncached if item.stable_id == decision.stable_id
                    )
                    _screening_cache[_cache_key(request, paper, config.ollama_model)] = value
        except (LLMError, ValueError) as exc:
            warnings.append(f"LLM screening fallback: {exc}")

    weight_sum = config.ranking_lexical_weight + config.ranking_llm_weight
    lexical_weight = config.ranking_lexical_weight / weight_sum if weight_sum else 1.0
    llm_weight = config.ranking_llm_weight / weight_sum if weight_sum else 0.0
    ranked: list[RankedPaper] = []
    for paper, lexical_value in lexical:
        llm = llm_scores.get(paper.stable_id)
        if llm:
            if not llm[0]:
                continue
            final = lexical_weight * lexical_value + llm_weight * llm[1]
            reason = llm[2]
            aspects = llm[3]
            llm_value = llm[1]
        else:
            final = lexical_value
            reason = "Ranked by deterministic title, abstract, keyword, and recency overlap"
            aspects = sorted(tokens(request.research_question) & tokens(paper.title))
            llm_value = None
        ranked.append(
            RankedPaper(
                paper=paper,
                lexical_score=lexical_value,
                llm_score=llm_value,
                final_score=round(min(1.0, max(0.0, final)), 6),
                selection_reason=reason,
                matched_aspects=aspects,
            )
        )
    ranked.sort(key=lambda item: (-item.final_score, item.paper.stable_id))
    return ranked, warnings


def _relevant_snippet(abstract: str, request: ResearchRequest, limit: int) -> str:
    if len(abstract) <= limit:
        return abstract
    lowered = abstract.casefold()
    terms = sorted(
        tokens(" ".join([request.research_question, *request.keywords])), key=len, reverse=True
    )
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    return abstract[start : start + limit]


def _cache_key(request: ResearchRequest, paper: PaperMetadata, model: str) -> str:
    content_hash = sha256(f"{paper.title}|{paper.abstract or ''}".encode()).hexdigest()
    value = f"{request.research_question.casefold()}|{paper.stable_id}|{content_hash}|{model}"
    return sha256(value.encode("utf-8")).hexdigest()
