import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.documents.errors import DocumentValidationError


class _TransientDownloadError(Exception):
    """Transport/timeout/rate-limit style failures worth retrying."""


class _LandingPageError(DocumentValidationError):
    """A public page was served instead of a PDF; keep the body to derive a file URL."""

    def __init__(self, page_url: str, html_body: str) -> None:
        super().__init__("Open access response is not application/pdf")
        self.page_url = page_url
        self.html_body = html_body


_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
_BLOCKED_STATUS = {403, 418}
_CITATION_PDF_RE = re.compile(r'citation_pdf_url"\s+content="([^"]+)"', re.IGNORECASE)
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']\s*\d+\s*;\s*url=([^"\'>]+)',
    re.IGNORECASE,
)


class OpenAccessDownloader:
    def __init__(self, max_bytes: int, *, max_redirects: int = 3,
                 timeout_seconds: float = 30, connect_timeout: float = 10,
                 max_retries: int = 2) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds
        self.connect_timeout = min(connect_timeout, timeout_seconds)
        self.max_retries = max_retries

    async def fetch(self, source_url: str) -> tuple[bytes, str]:
        """Download one open-access PDF, following derived file URLs on landing pages.

        Transport/timeout errors are retried up to ``max_retries`` times. Publisher
        blocks and pages without a derivable PDF fail fast with a readable message
        so the caller falls back to manual upload.
        """
        pending = [source_url]
        retries_left = self.max_retries
        errors: list[str] = []
        while pending:
            candidate = pending.pop(0)
            try:
                return await self._download_pdf(candidate)
            except _TransientDownloadError as exc:
                errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
                if retries_left <= 0:
                    break
                retries_left -= 1
                await asyncio.sleep(0.5 * (self.max_retries - retries_left + 1))
                pending.append(candidate)
            except _LandingPageError as exc:
                errors.append(f"{candidate}: 返回的是文章落地页而非 PDF 直链")
                variant = _pdf_variant_url(exc)
                if variant is None or variant == candidate:
                    break
                pending.insert(0, variant)
            except DocumentValidationError as exc:
                errors.append(f"{candidate}: {exc}")
                break
        message = "；".join(errors)
        raise DocumentValidationError(message[:2_000])

    async def _download_pdf(self, source_url: str) -> tuple[bytes, str]:
        current = source_url
        timeout = httpx.Timeout(self.timeout_seconds, connect=self.connect_timeout)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept": "application/pdf"},
        ) as client:
            for _ in range(self.max_redirects + 1):
                self._validate_public_https(current)
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise DocumentValidationError("PDF redirect has no location")
                            current = urljoin(current, location)
                            continue
                        if response.status_code in _TRANSIENT_STATUS:
                            raise _TransientDownloadError(f"HTTP {response.status_code}")
                        if response.status_code in _BLOCKED_STATUS:
                            raise DocumentValidationError(
                                f"出版商拦截自动下载（HTTP {response.status_code}），"
                                "请手动上传 PDF 或改用开放获取副本"
                            )
                        if response.status_code >= 400:
                            raise DocumentValidationError(f"HTTP {response.status_code}")
                        media_type = response.headers.get(
                            "content-type", ""
                        ).split(";", 1)[0].strip()
                        if media_type != "application/pdf":
                            body = await _read_html_body(response)
                            raise _LandingPageError(current, body)
                        length = response.headers.get("content-length")
                        if length and int(length) > self.max_bytes:
                            raise DocumentValidationError("Open access PDF exceeds the size limit")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise DocumentValidationError(
                                    "Open access PDF exceeds the size limit"
                                )
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        if not content.startswith(b"%PDF-"):
                            raise DocumentValidationError(
                                "Open access content has an invalid PDF signature"
                            )
                        return content, current
                except httpx.HTTPError as exc:
                    raise _TransientDownloadError(f"{type(exc).__name__}: {exc}") from exc
        raise DocumentValidationError("Open access PDF exceeded the redirect limit")

    @staticmethod
    def _validate_public_https(value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise DocumentValidationError("Open access URLs must be public HTTPS URLs")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443)}
        except OSError as exc:
            raise DocumentValidationError("Open access hostname cannot be resolved") from exc
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global:
                raise DocumentValidationError("Open access URL resolves to a non-public address")


async def _read_html_body(response: httpx.Response, cap: int = 1_000_000) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > cap:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="ignore")


def _pdf_variant_url(error: _LandingPageError) -> str | None:
    """Derive a probable direct PDF URL from a landing page, if any.

    Prefers explicit citation/meta-refresh links embedded in the page; for
    figshare-style article pages falls back to the ``/download`` endpoint.
    """
    for pattern in (_CITATION_PDF_RE, _META_REFRESH_RE):
        match = pattern.search(error.html_body)
        if match:
            target = urljoin(error.page_url, match.group(1).strip())
            if target != error.page_url:
                return target
    parsed = urlsplit(error.page_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    if host.endswith("figshare.com") and not path.endswith("/download") and path:
        return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/download", "", ""))
    return None
