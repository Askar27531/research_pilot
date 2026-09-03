import pytest

from app.mcp.literature import LiteratureCapabilityClient
from app.schemas import PaperMetadata


class _Router:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, capability, arguments, *args, result_adapter=None, **kwargs):
        self.calls.append((capability, arguments))
        if self.error is not None:
            raise self.error
        if result_adapter is not None:
            return result_adapter(self.result)
        return self.result


def _raw_arxiv_record() -> dict:
    return {
        "id": "2404.19756",
        "title": "KAN in registration",
        "authors": [{"name": "Ada Author"}, {"name": "Bob Builder"}],
        "published": "2024-04-30T12:00:00Z",
        "abstract": "We study Kolmogorov-Arnold networks for registration.",
    }


@pytest.mark.asyncio
async def test_get_arxiv_abstract_parses_external_record() -> None:
    router = _Router(_raw_arxiv_record())
    client = LiteratureCapabilityClient(router)

    result = await client.get_arxiv_abstract("2404.19756")

    assert router.calls == [("literature.metadata.arxiv", {"paper_id": "2404.19756"})]
    assert isinstance(result, PaperMetadata)
    assert result.stable_id == "arxiv:2404.19756"
    assert result.title == "KAN in registration"
    assert result.year == 2024
    assert result.abstract and result.abstract.startswith("We study")
    assert [author.name for author in result.authors] == ["Ada Author", "Bob Builder"]


@pytest.mark.asyncio
async def test_get_arxiv_abstract_returns_none_on_capability_failure() -> None:
    router = _Router(error=RuntimeError("external arxiv unavailable"))
    client = LiteratureCapabilityClient(router)

    assert await client.get_arxiv_abstract("2404.19756") is None


@pytest.mark.asyncio
async def test_get_arxiv_abstract_returns_none_on_unparsable_payload() -> None:
    router = _Router(result="not a paper record")
    client = LiteratureCapabilityClient(router)

    assert await client.get_arxiv_abstract("2404.19756") is None


@pytest.mark.asyncio
async def test_get_arxiv_abstract_skips_empty_identifier() -> None:
    router = _Router(_raw_arxiv_record())
    client = LiteratureCapabilityClient(router)

    assert await client.get_arxiv_abstract("") is None
    assert router.calls == []
