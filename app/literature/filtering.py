from dataclasses import dataclass

from app.literature.normalize import normalize_title
from app.schemas import PaperMetadata


@dataclass(slots=True)
class ConceptFilterResult:
    included: list[PaperMetadata]
    excluded: list[PaperMetadata]
    matched_concepts: dict[str, list[str]]


def filter_by_required_concepts(
    papers: list[PaperMetadata],
    required_groups: list[list[str]],
    excluded_topics: list[str],
) -> ConceptFilterResult:
    """Recall-first concept filter: keep any paper matching at least one group.

    Rejecting papers that hit none of the requested concept groups only removes
    obviously off-topic hits; precise relevance is decided by LLM screening.
    Papers matching an excluded topic are still dropped deterministically.
    """

    groups = [[_normalize(term) for term in group if _normalize(term)] for group in required_groups]
    groups = [group for group in groups if group]
    excluded_terms = [_normalize(term) for term in excluded_topics if _normalize(term)]
    included: list[PaperMetadata] = []
    excluded: list[PaperMetadata] = []
    matches: dict[str, list[str]] = {}

    for paper in papers:
        searchable = _normalize(f"{paper.title} {paper.abstract or ''}")
        matched_terms = [
            term
            for group in groups
            for term in group
            if _contains(searchable, term)
        ]
        has_required = not groups or bool(matched_terms)
        has_excluded = any(_contains(searchable, term) for term in excluded_terms)
        if has_required and not has_excluded:
            included.append(paper)
            matches[paper.stable_id] = matched_terms
        else:
            excluded.append(paper)
    return ConceptFilterResult(included, excluded, matches)


def _normalize(value: str) -> str:
    return normalize_title(value)


def _contains(searchable: str, term: str) -> bool:
    """Match normalized words or phrases without short-token substring false positives."""

    return f" {term} " in f" {searchable} "
