import pytest

from app.literature.providers import MultiSourceLiteratureProvider
from app.schemas import SearchResult


class _FailingProvider:
    name = "openalex"

    async def search_papers(self, *_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    async def close(self) -> None:
        return None


class _EmptyProvider:
    name = "crossref"

    async def search_papers(self, query, *_args, **_kwargs):
        return SearchResult(
            query=query,
            papers=[],
            total_available=0,
            source_latency_ms=1,
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_multisource_failure_preserves_provider_name() -> None:
    provider = MultiSourceLiteratureProvider({"openalex": _FailingProvider()})

    with pytest.raises(
        RuntimeError,
        match="All literature providers failed: openalex: RuntimeError: provider unavailable",
    ):
        await provider.search_papers("test query")


@pytest.mark.asyncio
async def test_multisource_empty_result_is_not_a_provider_failure() -> None:
    provider = MultiSourceLiteratureProvider({
        "openalex": _FailingProvider(),
        "crossref": _EmptyProvider(),
    })

    result = await provider.search_papers("no matching papers")

    assert result.papers == []
    assert result.total_available == 0
    assert result.warnings == ["openalex: RuntimeError: provider unavailable"]
