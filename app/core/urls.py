"""Public-URL validation shared by full-text discovery and PDF acquisition.

Both subsystems must agree on one question — "could this URL be downloaded at
all?" — because a mismatch makes the selection page promise a download the
downloader will refuse. This module is deliberately dependency-free (stdlib
only, no ``app.*`` imports) so ``app.documents`` and ``app.literature`` can both
use it without depending on each other.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def is_https_shaped_url(url: str) -> bool:
    """Shape-only check: public HTTPS with no embedded credentials, no DNS.

    Cheap enough to run over every candidate on the paper picker, and it
    already rejects the entries that dominate real metadata — OpenAlex stores
    ``http://`` locations and bare repository handles — which the downloader
    would otherwise fail on.
    """
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    )


def is_public_https_url(url: str) -> bool:
    """Full acquisition gate: shape plus a DNS check that resolves publicly.

    The resolution step is the SSRF guard — a hostname pointing at loopback or
    a private range must never be fetched — so it belongs on the download path
    rather than in prediction, where one lookup per candidate would dominate
    the page load.
    """
    if not is_https_shaped_url(url):
        return False
    parsed = urlsplit(url)
    assert parsed.hostname is not None  # guaranteed by is_https_shaped_url
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443)}
    except OSError:
        return False
    return all(ipaddress.ip_address(raw).is_global for raw in addresses)
