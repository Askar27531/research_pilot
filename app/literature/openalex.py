import asyncio
from time import perf_counter
from typing import Any, Self
from urllib.parse import quote

import httpx

from app.core.config import Settings, get_settings
from app.literature.errors import (
    LiteratureRateLimitError,
    LiteratureResponseError,
    LiteratureUnavailableError,
    OpenAlexAuthRequiredError,
    PaperNotFoundError,
)
from app.literature.normalize import map_openalex_work
from app.schemas import PaperMetadata, SearchPapersInput, SearchResult


class OpenAlexClient:
    """Async OpenAlex API client with bounded retries and business-model mapping."""

    name = "openalex"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.openalex_base_url.rstrip("/"),
            timeout=self.settings.openalex_timeout_seconds,
            headers={"User-Agent": "ResearchPilot/0.1"},
        )

    async def search_papers(
        self,
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
        trace_id: str | None = None,
    ) -> SearchResult:
        request = SearchPapersInput(query=query, year_from=year_from, year_to=year_to, limit=limit)
        params: dict[str, str | int] = {"search": request.query, "per-page": request.limit}
        filters: list[str] = []
        if request.year_from is not None:
            filters.append(f"from_publication_date:{request.year_from}-01-01")
        if request.year_to is not None:
            filters.append(f"to_publication_date:{request.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        self._add_identity_params(params)

        started = perf_counter()
        payload = await self._get_json("/works", params=params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise LiteratureResponseError("OpenAlex response did not contain a results list")
        papers = [map_openalex_work(work, request.query) for work in results]
        meta = payload.get("meta") or {}
        return SearchResult(
            query=request.query,
            papers=papers,
            total_available=max(0, int(meta.get("count") or len(papers))),
            source_latency_ms=max(0, round((perf_counter() - started) * 1000)),
        )

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata:
        value = identifier.strip()
        if not value:
            raise ValueError("identifier must not be empty")
        if value.lower().startswith("10."):
            value = f"https://doi.org/{value.lower()}"
        params: dict[str, str | int] = {}
        self._add_identity_params(params)
        payload = await self._get_json(f"/works/{quote(value, safe='')}", params=params)
        return map_openalex_work(payload)

    def _add_identity_params(self, params: dict[str, str | int]) -> None:
        api_key = (self.settings.openalex_api_key or "").strip()
        if not api_key:
            raise OpenAlexAuthRequiredError(
                "OPENALEX_API_KEY is required; create a free key at "
                "https://openalex.org/settings/api"
            )
        params["api_key"] = api_key

    async def _get_json(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.settings.openalex_max_attempts + 1):
            try:
                url = f"{self.settings.openalex_base_url.rstrip('/')}{path}"
                response = await self._client.get(url, params=params)
                if response.status_code in {401, 403}:
                    raise OpenAlexAuthRequiredError(
                        "OpenAlex rejected OPENALEX_API_KEY; verify the key at "
                        "https://openalex.org/settings/api"
                    )
                if response.status_code == 404:
                    raise PaperNotFoundError(f"OpenAlex work not found: {path.rsplit('/', 1)[-1]}")
                if response.status_code == 429:
                    if attempt == self.settings.openalex_max_attempts:
                        raise LiteratureRateLimitError("OpenAlex rate limit exceeded")
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                if response.status_code in {502, 503, 504}:
                    if attempt == self.settings.openalex_max_attempts:
                        raise LiteratureUnavailableError(
                            f"OpenAlex returned HTTP {response.status_code}"
                        )
                    await self._backoff(attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise LiteratureResponseError("OpenAlex returned non-object JSON")
                return payload
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < self.settings.openalex_max_attempts:
                    await self._backoff(attempt)
                    continue
            except httpx.HTTPStatusError as exc:
                raise LiteratureResponseError(
                    f"OpenAlex returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                ) from exc
            except ValueError as exc:
                raise LiteratureResponseError(f"OpenAlex returned invalid JSON: {exc}") from exc
        raise LiteratureUnavailableError(f"Could not reach OpenAlex: {last_error}") from last_error

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                seconds = min(float(retry_after), 10.0)
            except ValueError:
                seconds = self.settings.openalex_backoff_seconds * (2 ** (attempt - 1))
        else:
            seconds = self.settings.openalex_backoff_seconds * (2 ** (attempt - 1))
        await asyncio.sleep(seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
