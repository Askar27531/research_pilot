import re
import unicodedata
from typing import Any

from app.schemas import OpenAccessLocation, PaperAuthor, PaperMetadata


def _text(value: Any) -> str | None:
    """Keep only non-empty strings from third-party payloads."""
    return value if isinstance(value, str) and value else None


def _map_oa_locations(work: dict[str, Any]) -> list[OpenAccessLocation]:
    """Collect every hosting location OpenAlex knows, de-duplicated by URL.

    ``best_oa_location`` is included alongside ``locations``: OpenAlex frequently
    points "best" at the publisher's own page, which is exactly the URL that
    answers automated downloads with 403. Acquisition needs the repository and
    preprint copies listed next to it so it can prefer those instead.
    """
    locations: list[OpenAccessLocation] = []
    seen: set[str] = set()
    for entry in [work.get("best_oa_location"), *(work.get("locations") or [])]:
        if not isinstance(entry, dict):
            continue
        pdf_url = _text(entry.get("pdf_url"))
        landing = _text(entry.get("landing_page_url"))
        url = pdf_url or landing
        if url is None or url in seen:
            continue
        seen.add(url)
        entry_source = entry.get("source")
        locations.append(
            OpenAccessLocation(
                url=url,
                pdf_url=pdf_url,
                landing_page_url=landing,
                host_type=_text(entry.get("host_type")),
                source_type=(
                    _text(entry_source.get("type"))
                    if isinstance(entry_source, dict)
                    else None
                ),
                version=_text(entry.get("version")),
                license=_text(entry.get("license")),
                is_oa=bool(entry.get("is_oa")),
            )
        )
    return locations


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
        publisher=source.get("host_organization_name"),
        citation_count=max(0, int(work.get("cited_by_count") or 0)),
        open_access_url=oa_url,
        oa_locations=_map_oa_locations(work),
        openalex_id=source_id,
        sources=["openalex"],
        source_records=[{"source": "openalex", "id": source_id}],
        source_queries=[query] if query else [],
    )
