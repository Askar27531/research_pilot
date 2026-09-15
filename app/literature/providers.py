import asyncio
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from time import monotonic, perf_counter
from typing import ClassVar, Protocol
from urllib.parse import quote

import httpx

from app.core.config import Settings, get_settings
from app.literature.errors import LiteratureResponseError, PaperNotFoundError
from app.literature.normalize import normalize_title
from app.schemas import PaperAuthor, PaperMetadata, SearchResult


class LiteratureProvider(Protocol):
    name: str

    async def search_papers(
        self, query: str, year_from: int | None, year_to: int | None, limit: int
    ) -> SearchResult: ...

    async def close(self) -> None: ...


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        response = await client.get(url, **kwargs)
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
            return response
        if attempt < 2:
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * 2**attempt
            await asyncio.sleep(delay)
    response.raise_for_status()
    return response


def bare_doi(value: str) -> str:
    """Strip a DOI URL/prefix down to the bare ``10.x/...`` form."""
    doi = value.strip()
    lowered = doi.casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            return doi[len(prefix):].strip()
    return doi


class CrossrefProvider:
    name = "crossref"

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=self.settings.literature_timeout_seconds)

    async def search_papers(self, query, year_from=None, year_to=None, limit=20) -> SearchResult:
        params: dict[str, str | int] = {"query.bibliographic": query, "rows": limit}
        filters = []
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        started = perf_counter()
        response = await _get_with_retry(
            self.client, f"{self.settings.crossref_base_url}/works", params=params
        )
        message = response.json().get("message", {})
        papers = [self._map(item, query) for item in message.get("items", [])]
        return SearchResult(query=query, papers=papers,
            total_available=max(0, int(message.get("total-results", len(papers)))),
            source_latency_ms=round((perf_counter() - started) * 1000))

    @staticmethod
    def _map(item: dict, query: str | None = None) -> PaperMetadata:
        doi = item.get("DOI")
        title = " ".join(item.get("title") or []) or "Untitled"
        parts = item.get("published-print") or item.get("published-online") or {}
        year = ((parts.get("date-parts") or [[None]])[0] or [None])[0]
        authors = [PaperAuthor(name=" ".join(filter(None, [a.get("given"), a.get("family")])))
                   for a in item.get("author", []) if a.get("given") or a.get("family")]
        record = {"source": "crossref", "id": doi, "score": item.get("score")}
        return PaperMetadata(stable_id=doi or f"crossref:{normalize_title(title)}:{year}",
            source_id=doi or normalize_title(title), title=title, authors=authors, year=year,
            abstract=item.get("abstract"), doi=doi,
            venue=html.unescape(" ".join(item.get("container-title") or [])).strip() or None,
            publisher=(item.get("publisher") or "").strip() or None,
            citation_count=max(0, int(item.get("is-referenced-by-count") or 0)),
            source="crossref", sources=["crossref"], source_records=[record],
            source_queries=[query] if query else [])

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata:
        """Resolve one DOI through Crossref's works endpoint.

        A 404 is a real answer ("Crossref does not have this DOI") and returns
        immediately, which is why ``_get_with_retry`` is not reusable here (it
        raises before the status can be inspected). Transient failures — transport
        errors, 429, 5xx — are retried with backoff: on a flaky link a single
        connect failure must not turn a resolvable DOI into a permanent miss.
        """
        doi = bare_doi(identifier)
        if not doi:
            raise ValueError("identifier must not be empty")
        url = f"{self.settings.crossref_base_url}/works/{quote(doi, safe='')}"
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.get(url)
            except httpx.TransportError:
                if attempt == attempts:
                    raise
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            if response.status_code == 404:
                raise PaperNotFoundError(f"Crossref has no record for DOI {doi}")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == attempts:
                    response.raise_for_status()
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            response.raise_for_status()
            message = (response.json() or {}).get("message")
            if not isinstance(message, dict) or not message:
                raise LiteratureResponseError(f"Crossref returned no metadata for DOI {doi}")
            return self._map(message)
        raise LiteratureResponseError(f"Crossref lookup failed for DOI {doi}")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class ArxivProvider:
    name = "arxiv"
    NS: ClassVar = {
        "atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"
    }

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=self.settings.literature_timeout_seconds)

    async def search_papers(self, query, year_from=None, year_to=None, limit=20) -> SearchResult:
        started = perf_counter()
        response = await _get_with_retry(self.client, self.settings.arxiv_base_url, params={
            "search_query": f"all:{query}", "start": 0, "max_results": limit,
            "sortBy": "relevance", "sortOrder": "descending",
        })
        response.raise_for_status()
        root = ET.fromstring(response.text)
        papers = []
        for entry in root.findall("atom:entry", self.NS):
            published = entry.findtext("atom:published", default="", namespaces=self.NS)
            year = int(published[:4]) if published[:4].isdigit() else None
            if ((year_from and year and year < year_from)
                    or (year_to and year and year > year_to)):
                continue
            identifier = entry.findtext("atom:id", default="", namespaces=self.NS).rsplit("/", 1)[-1]
            identifier = re.sub(r"v\d+$", "", identifier)
            doi = entry.findtext("arxiv:doi", default=None, namespaces=self.NS)
            title = " ".join(entry.findtext("atom:title", default="Untitled", namespaces=self.NS).split())
            links = {link.get("title"): link.get("href") for link in entry.findall("atom:link", self.NS)}
            authors = [PaperAuthor(name=" ".join(a.findtext("atom:name", default="", namespaces=self.NS).split()))
                       for a in entry.findall("atom:author", self.NS)]
            papers.append(PaperMetadata(stable_id=doi or f"arxiv:{identifier}", source_id=identifier,
                title=title, authors=authors, year=year,
                abstract=" ".join(entry.findtext("atom:summary", default="", namespaces=self.NS).split()) or None,
                doi=doi, arxiv_id=identifier, open_access_url=links.get("pdf"), source="arxiv",
                sources=["arxiv"], source_records=[{"source": "arxiv", "id": identifier}],
                source_queries=[query]))
        return SearchResult(query=query, papers=papers, total_available=len(papers),
            source_latency_ms=round((perf_counter() - started) * 1000))

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def merge_papers(papers: Sequence[PaperMetadata]) -> list[PaperMetadata]:
    merged: dict[str, PaperMetadata] = {}
    for paper in papers:
        key = (f"doi:{paper.doi}" if paper.doi else f"arxiv:{paper.arxiv_id}"
               if paper.arxiv_id else f"title:{normalize_title(paper.title)}:{paper.year}")
        current = merged.get(key)
        if current is None:
            merged[key] = paper.model_copy(update={"sources": paper.sources or [paper.source]})
            continue
        sources = list(dict.fromkeys([*current.sources, *(paper.sources or [paper.source])]))
        records = [*current.source_records, *paper.source_records]
        queries = list(dict.fromkeys([*current.source_queries, *paper.source_queries]))
        merged[key] = current.model_copy(update={
            "abstract": current.abstract or paper.abstract,
            "doi": current.doi or paper.doi, "arxiv_id": current.arxiv_id or paper.arxiv_id,
            "open_access_url": current.open_access_url or paper.open_access_url,
            # Journal / publisher: keep whichever source actually resolved them, so
            # an arXiv-first record cannot blank out what Crossref or OpenAlex knew.
            "venue": current.venue or paper.venue,
            "publisher": current.publisher or paper.publisher,
            "citation_count": max(current.citation_count, paper.citation_count),
            "sources": sources, "source_records": records, "source_queries": queries,
        })
    return list(merged.values())


class MultiSourceLiteratureProvider:
    def __init__(self, providers: dict[str, LiteratureProvider]):
        self.providers = providers
        self._cache: dict[tuple, tuple[float, SearchResult]] = {}

    async def search_papers(self, query: str, year_from=None, year_to=None, limit=20,
                            sources: Sequence[str] | None = None) -> SearchResult:
        names = tuple(name for name in (sources or self.providers) if name in self.providers)
        cache_key = (query.casefold().strip(), year_from, year_to, limit, names)
        cached = self._cache.get(cache_key)
        if cached and monotonic() - cached[0] < get_settings().literature_cache_ttl_seconds:
            return cached[1].model_copy(deep=True)
        selected = [self.providers[name] for name in names]
        results = await asyncio.gather(
            *(p.search_papers(query, year_from, year_to, limit) for p in selected),
            return_exceptions=True,
        )
        papers, warnings, latency, successful_providers = [], [], 0, 0
        for provider, result in zip(selected, results, strict=True):
            if isinstance(result, BaseException):
                warnings.append(f"{provider.name}: {type(result).__name__}: {result}")
            else:
                successful_providers += 1
                papers.extend(result.papers)
                latency = max(latency, result.source_latency_ms)
        if successful_providers == 0:
            raise RuntimeError("All literature providers failed: " + "; ".join(warnings))
        merged = merge_papers(papers)[:limit]
        value = SearchResult(query=query, papers=merged, total_available=len(merged),
                             warnings=warnings, source_latency_ms=latency)
        self._cache[cache_key] = (monotonic(), value)
        return value

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self.providers.values()))
