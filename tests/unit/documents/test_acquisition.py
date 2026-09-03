"""OpenAccessDownloader resilience: retries, landing-page derivation, errors."""

import pytest

from app.documents.acquisition import (
    OpenAccessDownloader,
    _LandingPageError,
    _pdf_variant_url,
    _TransientDownloadError,
)
from app.documents.errors import DocumentValidationError


class _ScriptedDownloader(OpenAccessDownloader):
    """Override the network step with a scripted sequence."""

    def __init__(self, *behaviours) -> None:
        super().__init__(max_bytes=10**6, max_retries=2, timeout_seconds=30)
        self.behaviours = list(behaviours)
        self.calls: list[str] = []

    async def _download_pdf(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        behaviour = self.behaviours.pop(0)
        if callable(behaviour):
            return behaviour(url)
        raise behaviour


@pytest.mark.asyncio
async def test_transient_failure_is_retried_then_succeeds() -> None:
    downloader = _ScriptedDownloader(
        _TransientDownloadError("flaky"),
        lambda url: (b"%PDF-1.4 content", url),
    )

    content, final = await downloader.fetch("https://example.org/a.pdf")

    assert final == "https://example.org/a.pdf"
    assert content.startswith(b"%PDF-")
    assert downloader.calls == ["https://example.org/a.pdf", "https://example.org/a.pdf"]


@pytest.mark.asyncio
async def test_terminal_validation_error_is_not_retried() -> None:
    downloader = _ScriptedDownloader(DocumentValidationError("publisher blocks auto download"))

    with pytest.raises(DocumentValidationError) as excinfo:
        await downloader.fetch("https://publisher.example/a.pdf")

    assert "publisher blocks auto download" in str(excinfo.value)
    assert downloader.calls == ["https://publisher.example/a.pdf"]


@pytest.mark.asyncio
async def test_figshare_landing_page_falls_back_to_download_endpoint() -> None:
    landing = "https://figshare.com/articles/journal_contribution/Some_Paper/28934963"

    def behave(url: str) -> tuple[bytes, str]:
        if url == f"{landing}/download":
            return b"%PDF-1.4 content", url
        raise _LandingPageError(url, "<html>article page</html>")

    downloader = _ScriptedDownloader(behave, behave)

    content, final = await downloader.fetch(landing)

    assert content.startswith(b"%PDF-")
    assert final == f"{landing}/download"
    assert downloader.calls == [landing, f"{landing}/download"]


def test_pdf_variant_prefers_citation_link() -> None:
    body = (
        '<meta name="citation_pdf_url" content="https://files.example/paper.pdf">'
    )
    error = _LandingPageError("https://journal.example/article/1", body)

    assert _pdf_variant_url("https://journal.example/article/1", error) == (
        "https://files.example/paper.pdf"
    )


def test_pdf_variant_uses_figshare_download_endpoint() -> None:
    url = "https://figshare.com/articles/journal_contribution/Paper/28934963"
    error = _LandingPageError(url, "<html></html>")

    assert _pdf_variant_url(url, error) == f"{url}/download"


def test_pdf_variant_returns_none_without_derivable_link() -> None:
    url = "https://other.example/article/1"
    error = _LandingPageError(url, "<html></html>")

    assert _pdf_variant_url(url, error) is None
