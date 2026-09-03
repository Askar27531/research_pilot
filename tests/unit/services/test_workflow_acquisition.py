import pytest

from app.schemas import PaperMetadata
from app.services.workflow import _arxiv_pdf_url, _resolve_open_access_url


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


def test_arxiv_pdf_url_uses_arxiv_id_without_version() -> None:
    paper = _paper(
        source="arxiv", sources=["arxiv"], arxiv_id="2404.19756",
        stable_id="arxiv:2404.19756", source_id="2404.19756", doi=None,
    )

    assert _arxiv_pdf_url(paper) == "https://arxiv.org/pdf/2404.19756"


def test_arxiv_pdf_url_falls_back_to_stable_id() -> None:
    paper = _paper(
        source="arxiv", sources=["arxiv"], arxiv_id=None, doi=None,
        stable_id="arxiv:2301.12345", source_id="2301.12345",
    )

    assert _arxiv_pdf_url(paper) == "https://arxiv.org/pdf/2301.12345"


def test_arxiv_pdf_url_none_for_non_arxiv_paper() -> None:
    paper = _paper(source="crossref", doi="10.1000/example")

    assert _arxiv_pdf_url(paper) is None
