import pytest

from app.schemas import PaperMetadata
from app.services.workflow import _resolve_open_access_url


class _MetadataClient:
    def __init__(self, result: PaperMetadata) -> None:
        self.result = result
        self.identifiers: list[str] = []

    async def get_paper_metadata(self, identifier: str) -> PaperMetadata:
        self.identifiers.append(identifier)
        return self.result


def _paper(**updates) -> PaperMetadata:
    values = {
        "stable_id": "doi:10.1000/example",
        "source_id": "10.1000/example",
        "title": "Example paper",
        "doi": "10.1000/example",
        "source": "crossref",
    }
    values.update(updates)
    return PaperMetadata(**values)


@pytest.mark.asyncio
async def test_resolve_open_access_url_enriches_doi_only_record() -> None:
    client = _MetadataClient(_paper(
        source="openalex",
        open_access_url="https://example.org/paper.pdf",
    ))

    url, error = await _resolve_open_access_url(client, _paper())

    assert url == "https://example.org/paper.pdf"
    assert error is None
    assert client.identifiers == ["10.1000/example"]


@pytest.mark.asyncio
async def test_resolve_open_access_url_explains_missing_public_pdf() -> None:
    client = _MetadataClient(_paper(source="openalex"))

    url, error = await _resolve_open_access_url(client, _paper())

    assert url is None
    assert error == "已通过 DOI 10.1000/example 补查全文，但未发现开放获取 PDF。"
