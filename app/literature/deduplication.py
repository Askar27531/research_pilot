from dataclasses import dataclass

from app.literature.normalize import normalize_title
from app.schemas import PaperMetadata


@dataclass(slots=True)
class DeduplicationResult:
    unique_papers: list[PaperMetadata]
    duplicate_groups: list[list[str]]
    merge_log: list[str]


def paper_key(paper: PaperMetadata) -> str:
    if paper.doi:
        return f"doi:{paper.doi}"
    if paper.source_id:
        return f"openalex:{paper.source_id.casefold()}"
    return f"title:{normalize_title(paper.title)}:{paper.year or 'unknown'}"


def deduplicate_papers(papers: list[PaperMetadata]) -> DeduplicationResult:
    grouped: dict[str, list[PaperMetadata]] = {}
    order: list[str] = []
    for paper in papers:
        key = paper_key(paper)
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(paper)

    unique: list[PaperMetadata] = []
    duplicate_groups: list[list[str]] = []
    merge_log: list[str] = []
    for key in order:
        group = grouped[key]
        merged = group[0].model_copy(deep=True)
        for candidate in group[1:]:
            if len(candidate.abstract or "") > len(merged.abstract or ""):
                merged.abstract = candidate.abstract
            if not merged.doi and candidate.doi:
                merged.doi = candidate.doi
            merged.citation_count = max(merged.citation_count, candidate.citation_count)
            merged.source_queries = list(
                dict.fromkeys([*merged.source_queries, *candidate.source_queries])
            )
        unique.append(merged)
        if len(group) > 1:
            ids = [paper.source_id for paper in group]
            duplicate_groups.append(ids)
            merge_log.append(f"Merged {len(group)} records using {key}")
    return DeduplicationResult(unique, duplicate_groups, merge_log)
