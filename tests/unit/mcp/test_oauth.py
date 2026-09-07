"""Offline tests for mcp_servers/oauth auth-mode selection and token verification.

These tests never touch the network: the GitHub provider is stubbed at the
factory boundary and ``GitHubTokenVerifier`` is exercised through an httpx
``MockTransport``. The live GitHub OAuth dance is covered by docs/MCP_OAUTH.md.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

import mcp_servers.oauth as oauth_mod
from mcp_servers.oauth import (
    CONSENT_ENV,
    GITHUB_BASE_URL_ENV,
    GITHUB_CLIENT_ID_ENV,
    GITHUB_CLIENT_SECRET_ENV,
    STATIC_TOKEN_ENV,
    StaticBearerTokenVerifier,
    build_auth,
)


class _GitHubStub:
    """Records constructor kwargs; never touches GitHub."""

    instances: ClassVar[list[dict]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        _GitHubStub.instances.append(kwargs)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (STATIC_TOKEN_ENV, GITHUB_CLIENT_ID_ENV, GITHUB_CLIENT_SECRET_ENV, GITHUB_BASE_URL_ENV, CONSENT_ENV):
        monkeypatch.delenv(name, raising=False)


def test_loopback_defaults_to_no_auth() -> None:
    auth, description = build_auth(None, host="127.0.0.1")
    assert auth is None
    assert "none" in description


def test_non_loopback_default_requires_static_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=STATIC_TOKEN_ENV):
        build_auth(None, host="0.0.0.0")

    monkeypatch.setenv(STATIC_TOKEN_ENV, "s3cret")
    auth, description = build_auth(None, host="0.0.0.0")
    assert isinstance(auth, StaticBearerTokenVerifier)
    assert "static" in description


async def test_static_verifier_accepts_expected_token_only() -> None:
    verifier = StaticBearerTokenVerifier("expected", "http://127.0.0.1:8100")
    granted = await verifier.verify_token("expected")
    assert granted is not None
    assert granted.client_id == "researchpilot-mcp"
    assert await verifier.verify_token("wrong") is None


def test_github_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_CLIENT_ID_ENV, "Ov23li123")
    with pytest.raises(ValueError, match=GITHUB_CLIENT_SECRET_ENV):
        build_auth("github", host="127.0.0.1", public_base_url="http://127.0.0.1:8102")


def test_github_builds_provider_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_mod, "GitHubProvider", _GitHubStub)
    monkeypatch.setenv(GITHUB_CLIENT_ID_ENV, "Ov23li123")
    monkeypatch.setenv(GITHUB_CLIENT_SECRET_ENV, "sec")
    auth, description = build_auth(
        "github", host="127.0.0.1", public_base_url="http://127.0.0.1:8102"
    )
    assert isinstance(auth, _GitHubStub)
    assert "github" in description
    recorded = _GitHubStub.instances[-1]
    assert recorded["client_id"] == "Ov23li123"
    assert recorded["client_secret"] == "sec"
    assert recorded["base_url"] == "http://127.0.0.1:8102"
    assert recorded["required_scopes"] == ["user"]
    assert recorded["require_authorization_consent"] is True
    assert recorded["cache_ttl_seconds"] == 300


def test_github_consent_env_can_disable_consent_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_mod, "GitHubProvider", _GitHubStub)
    monkeypatch.setenv(GITHUB_CLIENT_ID_ENV, "id")
    monkeypatch.setenv(GITHUB_CLIENT_SECRET_ENV, "sec")
    monkeypatch.setenv(CONSENT_ENV, "0")
    build_auth("github", host="127.0.0.1", public_base_url="http://127.0.0.1:8102")
    assert _GitHubStub.instances[-1]["require_authorization_consent"] is False


def test_github_rejects_plain_http_for_non_loopback_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GITHUB_CLIENT_ID_ENV, "id")
    monkeypatch.setenv(GITHUB_CLIENT_SECRET_ENV, "sec")
    with pytest.raises(ValueError, match="https"):
        build_auth("github", host="0.0.0.0", public_base_url="http://mcp.example.com:8100")


def _api_handler(request: httpx.Request, *, user_status: int, scopes_header: str):
    if request.url.path == "/user":
        return httpx.Response(
            user_status,
            json={"id": 123, "login": "octocat", "name": "Octo Cat"},
        )
    # Any authenticated endpoint used to read X-OAuth-Scopes.
    return httpx.Response(200, json=[], headers={"X-OAuth-Scopes": scopes_header})


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_github_token_verifier_happy_path() -> None:
    client = _client_with(
        lambda request: _api_handler(request, user_status=200, scopes_header="user")
    )
    try:
        verifier = GitHubTokenVerifier(http_client=client)
        granted = await verifier.verify_token("ghp_x")
    finally:
        await client.aclose()
    assert granted is not None
    assert granted.client_id == "123"
    assert granted.claims["login"] == "octocat"
    assert "user" in granted.scopes


async def test_github_token_verifier_rejects_invalid_token() -> None:
    client = _client_with(
        lambda request: _api_handler(request, user_status=401, scopes_header="")
    )
    try:
        verifier = GitHubTokenVerifier(http_client=client)
        assert await verifier.verify_token("ghp_bad") is None
    finally:
        await client.aclose()


async def test_github_token_verifier_enforces_required_scopes() -> None:
    client = _client_with(
        lambda request: _api_handler(request, user_status=200, scopes_header="user")
    )
    try:
        verifier = GitHubTokenVerifier(required_scopes=["repo"], http_client=client)
        assert await verifier.verify_token("ghp_x") is None
    finally:
        await client.aclose()


async def test_github_token_verifier_tolerates_network_failure() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with(failing)
    try:
        verifier = GitHubTokenVerifier(http_client=client)
        assert await verifier.verify_token("ghp_x") is None
    finally:
        await client.aclose()
