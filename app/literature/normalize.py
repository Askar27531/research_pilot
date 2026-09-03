import re
import unicodedata
from typing import Any

from app.schemas import PaperAuthor, PaperMetadata


def normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", title).casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positioned = ((position, word) for word, positions in index.items() for position in positions)
    words = [word for _, word in sorted(positioned)]
    return " ".join(words) or None


def map_openalex_work(work: dict[str, Any], query: str | None = None) -> PaperMetadata:
    source_id = str(work.get("id") or "").removeprefix("https://openalex.org/")
    doi = work.get("doi")
    stable_id = str(doi or source_id)
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    best_oa = work.get("best_oa_location") or {}
    oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(PaperAuthor(name=author["display_name"], orcid=author.get("orcid")))
    return PaperMetadata(
        stable_id=stable_id,
        source_id=source_id,
        title=work.get("display_name") or work.get("title") or "Untitled",
        authors=authors,
        year=work.get("publication_year"),
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        doi=doi,
        venue=source.get("display_name"),
        citation_count=max(0, int(work.get("cited_by_count") or 0)),
        open_access_url=oa_url,
        openalex_id=source_id,
        sources=["openalex"],
        source_records=[{"source": "openalex", "id": source_id}],
        source_queries=[query] if query else [],
    )
