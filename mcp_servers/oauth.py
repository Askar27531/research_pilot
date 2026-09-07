"""Auth provider selection for self-hosted ResearchPilot MCP servers.

The FastMCP servers in this package can be protected in one of three ways when
they are exposed over HTTP:

* ``none``   – no authentication (loopback / trusted network only);
* ``static`` – a pre-shared Bearer token (``MCP_AUTH_TOKEN``);
* ``github`` – full OAuth 2.1 authorization-code flow with PKCE, proxied to a
               GitHub OAuth App through FastMCP's ``GitHubProvider``.

``github`` is the mode an external OAuth-capable MCP client (Claude Desktop /
Claude Code / any spec-compliant client) uses: the client reads the
authorization-server metadata, registers as a client, opens ``/authorize``,
the user approves on GitHub, and this server mints short-lived tokens which it
then validates against ``https://api.github.com`` on every request.

Only the HTTP transports are affected. ``stdio`` servers stay unauthenticated,
which keeps in-process consumption of the same factories unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

GITHUB_CLIENT_ID_ENV = "MCP_GITHUB_CLIENT_ID"
GITHUB_CLIENT_SECRET_ENV = "MCP_GITHUB_CLIENT_SECRET"
GITHUB_BASE_URL_ENV = "MCP_OAUTH_BASE_URL"
STATIC_TOKEN_ENV = "MCP_AUTH_TOKEN"
CONSENT_ENV = "MCP_OAUTH_REQUIRE_CONSENT"
JWT_SIGNING_KEY_ENV = "MCP_OAUTH_JWT_SIGNING_KEY"
OAUTH_STORAGE_DIR_ENV = "MCP_OAUTH_DATA_DIR"

GITHUB_REQUIRED_SCOPES = ["user"]

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STORAGE_DIR = _PROJECT_ROOT / "data" / "oauth-proxy"


class StaticBearerTokenVerifier(TokenVerifier):
    """Small single-user verifier for self-hosted non-loopback MCP endpoints.

    This is the pre-OAuth fallback: an endpoint protected this way expects the
    exact token in ``MCP_AUTH_TOKEN`` on every request. It is not a substitute
    for an OAuth authorization-code flow when real MCP clients are involved.
    """

    def __init__(self, expected: str, base_url: str) -> None:
        super().__init__(base_url=base_url)
        self.expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self.expected):
            return None
        return AccessToken(token=token, client_id="researchpilot-mcp", scopes=[])


def is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _public_url_http_allowed(url: str) -> bool:
    """http:// is only allowed for loopback URLs (GitHub local-dev callbacks)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return True
    host = parsed.hostname or ""
    return is_loopback(host)


def env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _require_env(name: str, purpose: str) -> str:
    value = env(name)
    if value is None:
        raise ValueError(f"{name} is required for {purpose}")
    return value


def _storage_encryption_key(material: bytes) -> bytes:
    """Deterministic Fernet key so OAuth state survives server restarts."""
    return base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac(
            "sha256", material, b"researchpilot-mcp-oauth-storage", 200_000, dklen=32
        )
    )


def _build_client_storage(base_dir: Path, encryption_key: bytes):
    """Encrypted-at-rest key-value store for OAuth client registrations/tokens.

    Mirrors the store FastMCP would otherwise create under the user data
    directory, but keeps all state inside the project (default
    ``data/oauth-proxy``) so nothing is written outside the workspace.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    file_store = FileTreeStore(
        data_directory=base_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(base_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            base_dir
        ),
    )
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=encryption_key),
        raise_on_decryption_error=False,
    )


def build_github_provider(
    *,
    client_id: str,
    client_secret: str,
    base_url: str,
    required_scopes: list[str] | None = None,
    require_consent: bool = True,
    cache_ttl_seconds: int | None = 300,
    jwt_signing_key: str | bytes | None = None,
    storage_dir: Path | None = None,
) -> GitHubProvider:
    """Build the FastMCP GitHub OAuth proxy used to protect an HTTP endpoint.

    OAuth state (client registrations, refresh tokens) is persisted encrypted
    under ``storage_dir`` (default ``data/oauth-proxy``), isolated per signing
    key so multiple servers on the same machine do not collide.
    """
    storage_root = Path(storage_dir or env(OAUTH_STORAGE_DIR_ENV) or _DEFAULT_STORAGE_DIR)
    signing_material = (
        jwt_signing_key
        if isinstance(jwt_signing_key, bytes)
        else (jwt_signing_key or client_secret).encode("utf-8")
    )
    encryption_key = _storage_encryption_key(signing_material)
    fingerprint = hashlib.sha256(encryption_key).hexdigest()[:12]
    client_storage = _build_client_storage(storage_root / fingerprint, encryption_key)
    return GitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        required_scopes=required_scopes or GITHUB_REQUIRED_SCOPES,
        require_authorization_consent=require_consent,
        cache_ttl_seconds=cache_ttl_seconds,
        jwt_signing_key=jwt_signing_key,
        client_storage=client_storage,
    )


def build_auth(
    mode: str | None,
    *,
    host: str,
    public_base_url: str | None = None,
) -> tuple[Any | None, str]:
    """Resolve the HTTP auth mode into a FastMCP ``auth`` object.

    Args:
        mode: ``"none"``, ``"static"``, ``"github"`` or ``None`` for auto:
            loopback hosts get no auth, non-loopback hosts require the static
            ``MCP_AUTH_TOKEN`` (previous behaviour).
        host: the bind host; loopback hosts default to no auth.
        public_base_url: public URL of this MCP endpoint. Required for
            ``github``; falls back to ``MCP_OAUTH_BASE_URL``.

    Returns:
        ``(auth, description)`` where ``auth`` is the FastMCP auth provider (or
        ``None``) and ``description`` is a short human-readable label for logs.
    """
    loopback = is_loopback(host)
    effective = mode or ("none" if loopback else "static")

    if effective == "none":
        return None, "none (loopback / trusted network)"

    if effective == "static":
        token = env(STATIC_TOKEN_ENV)
        if not token:
            raise ValueError(
                f"{STATIC_TOKEN_ENV} is required for --auth static on a "
                f"non-loopback bind ({host!r}); set it or choose --auth none / "
                "--auth github"
            )
        return StaticBearerTokenVerifier(token, f"http://{host}"), "static Bearer token"

    if effective == "github":
        client_id = _require_env(GITHUB_CLIENT_ID_ENV, "--auth github")
        client_secret = _require_env(GITHUB_CLIENT_SECRET_ENV, "--auth github")
        base_url = public_base_url or env(GITHUB_BASE_URL_ENV)
        if not base_url:
            raise ValueError(
                "a public base URL is required for --auth github: pass "
                "--public-base-url or set " + GITHUB_BASE_URL_ENV
            )
        if not _public_url_http_allowed(base_url):
            raise ValueError(
                "public base URL must be https:// for non-loopback endpoints; "
                "http:// is only allowed for loopback development"
            )
        consent_raw = env(CONSENT_ENV)
        require_consent = True if consent_raw is None else consent_raw.lower() not in {"0", "false", "no"}
        auth = build_github_provider(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            jwt_signing_key=env(JWT_SIGNING_KEY_ENV),
            require_consent=require_consent,
        )
        return auth, f"github OAuth 2.1 (client {client_id[:8]}… at {base_url})"

    raise ValueError(f"unknown auth mode: {mode!r}")


__all__ = [
    "CONSENT_ENV",
    "GITHUB_BASE_URL_ENV",
    "GITHUB_CLIENT_ID_ENV",
    "GITHUB_CLIENT_SECRET_ENV",
    "JWT_SIGNING_KEY_ENV",
    "OAUTH_STORAGE_DIR_ENV",
    "STATIC_TOKEN_ENV",
    "StaticBearerTokenVerifier",
    "build_auth",
    "build_github_provider",
    "is_loopback",
]
