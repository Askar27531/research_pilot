import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.documents.errors import DocumentValidationError


class OpenAccessDownloader:
    def __init__(self, max_bytes: int, *, max_redirects: int = 3,
                 timeout_seconds: float = 10) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds

    async def fetch(self, source_url: str) -> tuple[bytes, str]:
        current = source_url
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(5, self.timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(self.max_redirects + 1):
                self._validate_public_https(current)
                async with client.stream("GET", current, headers={"Accept": "application/pdf"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise DocumentValidationError("PDF redirect has no location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    if media_type != "application/pdf":
                        raise DocumentValidationError("Open access response is not application/pdf")
                    length = response.headers.get("content-length")
                    if length and int(length) > self.max_bytes:
                        raise DocumentValidationError("Open access PDF exceeds the size limit")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise DocumentValidationError("Open access PDF exceeds the size limit")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if not content.startswith(b"%PDF-"):
                        raise DocumentValidationError("Open access content has an invalid PDF signature")
                    return content, current
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
