import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any

from app.literature.providers import merge_papers
from app.mcp.registry import CapabilityRouter
from app.schemas import PaperAuthor, PaperMetadata, SearchResult


def _unwrap(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return _unwrap(json.loads(value))
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict):
        if "papers" in value:
            return value
        for key in ("result", "data", "content", "structuredContent"):
            if key in value:
                candidate = _unwrap(value[key])
                if isinstance(candidate, dict) and "papers" in candidate:
                    return candidate
        for candidate in value.values():
            unwrapped = _unwrap(candidate)
            if isinstance(unwrapped, dict) and "papers" in unwrapped:
                return unwrapped
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "text" in item:
                candidate = _unwrap(item["text"])
                if isinstance(candidate, dict):
                    return candidate
            candidate = _unwrap(item)
            if isinstance(candidate, dict) and "papers" in candidate:
                return candidate
    return value


def normalize_arxiv_search(value: Any, query: str) -> SearchResult:
    payload = _unwrap(value)
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("papers"), list)
        and (not payload["papers"] or "stable_id" in payload["papers"][0])
    ):
        return SearchResult.model_validate(payload)
    if isinstance(payload, dict) and payload.get("status") in {"error", "rate_limited"}:
        raise RuntimeError(payload.get("message", "External arXiv search failed"))
    if not isinstance(payload, dict) or not isinstance(payload.get("papers"), list):
        raise TypeError("External arXiv MCP returned no structured papers")
    papers = []
    for item in payload["papers"]:
        identifier = str(item.get("id") or item.get("paper_id") or "").strip()
        if not identifier:
            continue
        identifier = identifier.rstrip("/").rsplit("/", 1)[-1]
        identifier = re.sub(r"v\d+$", "", identifier)
        published = str(item.get("published") or "")
        year = int(published[:4]) if published[:4].isdigit() else None
        papers.append(PaperMetadata(
            stable_id=f"arxiv:{identifier}",
            source_id=identifier,
            title=str(item.get("title") or "Untitled"),
            authors=[PaperAuthor(name=str(
                author.get("name") if isinstance(author, dict) else author
            )) for author in item.get("authors", [])],
            year=year,
            abstract=item.get("abstract"),
            arxiv_id=identifier,
            open_access_url=item.get("url") or item.get("pdf_url"),
            source="arxiv",
            sources=["arxiv"],
            source_records=[{"source": "external-arxiv-mcp", "id": identifier}],
            source_queries=[query],
        ))
    return SearchResult(
        query=query,
        papers=papers,
        total_available=int(payload.get("total_results") or len(papers)),
        source_latency_ms=0,
    )


def _find_paper_payload(value: Any) -> dict[str, Any] | None:
    """Locate a tool-result dictionary that looks like one paper record."""
    if isinstance(value, dict):
        if isinstance(value.get("title"), str) and (
            "abstract" in value or "summary" in value or "published" in value or "id" in value
        ):
            return value
        for candidate in value.values():
            found = _find_paper_payload(candidate)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "text" in item:
                try:
                    parsed = json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    parsed = item["text"]
                found = _find_paper_payload(parsed)
                if found is not None:
                    return found
            found = _find_paper_payload(item)
            if found is not None:
                return found
    return None


def _arxiv_year(value: Any) -> int | None:
    for key in ("published", "date", "updated", "submitted"):
        raw = value.get(key) if isinstance(value, dict) else None
        if isinstance(raw, str) and raw[:4].isdigit():
            return int(raw[:4])
    return None


def _arxiv_abstract_metadata(arxiv_id: str, value: Any) -> PaperMetadata:
    """Parse the external `get_abstract` result into one arXiv paper record."""
    payload = _find_paper_payload(value)
    if not payload or not isinstance(payload.get("title"), str) or not payload["title"].strip():
        raise ValueError("External arXiv metadata returned no paper record")
    identifier = str(
        payload.get("paper_id") or payload.get("arxiv_id") or payload.get("id") or arxiv_id
    ).strip().rstrip("/").rsplit("/", 1)[-1]
    abstract = payload.get("abstract") or payload.get("summary")
    authors = [
        PaperAuthor(name=str(
            author.get("name") if isinstance(author, dict) else author
        ))
        for author in payload.get("authors", [])
        if str(author.get("name") if isinstance(author, dict) else author).strip()
    ]
    pdf_url = payload.get("pdf_url") or payload.get("open_access_url")
    return PaperMetadata(
        stable_id=f"arxiv:{identifier}",
        source_id=identifier,
        title=payload["title"].strip(),
        authors=authors,
        year=_arxiv_year(payload),
        abstract=abstract if isinstance(abstract, str) else None,
        arxiv_id=identifier,
        open_access_url=pdf_url if isinstance(pdf_url, str) else None,
        source="arxiv",
        sources=["arxiv"],
        source_records=[{"source": "external-arxiv-mcp", "id": identifier}],
    )


class LiteratureCapabilityClient:
    """Stable literature port backed by the MCP capability router."""

    def __init__(self, router: CapabilityRouter) -> None:
        self.router = router

    async def _search(
        self, capability: str, query: str, year_from: int | None,
        year_to: int | None, limit: int, sources: Sequence[str],
    ) -> SearchResult:
        canonical = {
            "query": query, "year_from": year_from, "year_to": year_to,
            "limit": limit, "sources": list(sources),
        }
        external = {
            "query": query,
            "max_results": min(limit, 50),
            "abstract_mode": "full",
            **({"date_from": f"{year_from}-01-01"} if year_from else {}),
            **({"date_to": f"{year_to}-12-31"} if year_to else {}),
        }
        if capability == "literature.search.arxiv":
            value = await self.router.call(
                capability,
                canonical,
                {"external-arxiv": external},
                result_adapter=lambda result: normalize_arxiv_search(result, query),
            )
            return value
        value = await self.router.call(capability, canonical, {"external-arxiv": external})
        return SearchResult.model_validate(value)

    async def search_papers(
        self, query: str, year_from: int | None = None, year_to: int | None = None,
        limit: int = 20,
        sources: list[str] | None = None,
    ) -> SearchResult:
        selected = list(sources or ["openalex", "crossref", "arxiv"])
        tasks = []
        non_arxiv = [name for name in selected if name != "arxiv"]
        if non_arxiv:
            tasks.append(self._search(
                "literature.search.multisource", query, year_from, year_to, limit, non_arxiv,
            ))
        if "arxiv" in selected:
            tasks.append(self._search(
                "literature.search.arxiv", query, year_from, year_to, limit, ["arxiv"]
            ))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = [result for result in results if isinstance(result, SearchResult)]
        if not valid:
            details = "; ".join(
                f"{type(result).__name__}: {result}"
                for result in results if isinstance(result, BaseException)
            )
            raise RuntimeError(
                "All configured literature MCP capabilities failed"
                + (f": {details}" if details else "")
            )
        warnings = [f"{type(result).__name__}: {result}" for result in results
                    if isinstance(result, BaseException)]
        papers = merge_papers([paper for result in valid for paper in result.papers])[:limit]
        return SearchResult(
            query=query,
            papers=papers,
            total_available=len(papers),
            warnings=warnings,
            source_latency_ms=max((result.source_latency_ms for result in valid), default=0),
        )

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata:
        value = await self.router.call("literature.metadata", {"identifier": identifier})
        return PaperMetadata.model_validate(value)

    async def get_arxiv_abstract(self, arxiv_id: str) -> PaperMetadata | None:
        """Fetch authoritative arXiv metadata/abstract for screening enrichment.

        Returns None (never raises) when the external capability is unavailable
        or returns an unparsable payload; callers fall back to partial metadata.
        """
        if not arxiv_id:
            return None
        try:
            return await self.router.call(
                "literature.metadata.arxiv",
                {"paper_id": arxiv_id.strip()},
                result_adapter=lambda result: _arxiv_abstract_metadata(arxiv_id, result),
            )
        except Exception:  # noqa: BLE001 - enrichment is best effort
            return None
