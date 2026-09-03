import json
from pathlib import Path

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from app.mcp import (
    CapabilityRouter,
    MCPContractError,
    MCPGatewayConfig,
    MCPRegistry,
    normalize_arxiv_search,
)


def _config(*servers: dict) -> MCPGatewayConfig:
    return MCPGatewayConfig.model_validate({"version": 1, "servers": list(servers)})


def _server(server_id: str, priority: int) -> dict:
    return {
        "id": server_id,
        "source": "researchpilot",
        "transport": "inprocess",
        "capabilities": {"test.echo": {"tool": "echo", "priority": priority}},
    }


def test_config_rejects_duplicate_ids_and_literal_environment_values() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _config(_server("same-server", 1), _server("same-server", 2))
    payload = _server("safe-server", 1) | {"env_allowlist": ["TOKEN=secret"]}
    with pytest.raises(ValidationError):
        _config(payload)


def test_packaged_default_config_matches_editable_config() -> None:
    from importlib.resources import files

    editable = json.loads(Path("config/mcp_servers.json").read_text(encoding="utf-8"))
    packaged = json.loads(files("app.mcp").joinpath("default_servers.json").read_text())
    assert packaged == editable


def test_external_arxiv_result_is_normalized_to_stable_domain_schema() -> None:
    result = normalize_arxiv_search({
        "papers": [{
            "id": "https://arxiv.org/abs/2401.12345v2",
            "title": "A paper",
            "authors": [{"name": "Ada"}],
            "published": "2024-01-31",
            "abstract": "Abstract",
            "url": "https://arxiv.org/pdf/2401.12345",
        }]
    }, "paper")
    assert result.papers[0].stable_id == "arxiv:2401.12345"
    assert result.papers[0].authors[0].name == "Ada"


@pytest.mark.asyncio
async def test_router_uses_priority_and_records_temporary_fallback() -> None:
    primary = FastMCP("primary")
    fallback = FastMCP("fallback")

    @primary.tool(name="echo")
    def primary_echo(value: str) -> str:
        raise RuntimeError("temporarily unavailable")

    @fallback.tool(name="echo")
    def fallback_echo(value: str) -> str:
        return f"fallback:{value}"

    registry = MCPRegistry(
        _config(_server("primary-server", 10), _server("fallback-server", 20)),
        {"primary-server": primary, "fallback-server": fallback},
    )
    await registry.discover()
    result = await CapabilityRouter(registry).call("test.echo", {"value": "ok"})

    assert result == {"result": "fallback:ok"}
    assert registry.last_routes["test.echo"]["fallback_from"] == "primary-server"
    assert registry.last_routes["test.echo"]["fallback_to"] == "fallback-server"


@pytest.mark.asyncio
async def test_router_falls_back_when_server_result_cannot_be_adapted() -> None:
    primary = FastMCP("primary")
    fallback = FastMCP("fallback")

    @primary.tool(name="echo")
    def primary_echo(value: str) -> str:
        return "malformed"

    @fallback.tool(name="echo")
    def fallback_echo(value: str) -> str:
        return f"valid:{value}"

    registry = MCPRegistry(
        _config(_server("primary-server", 10), _server("fallback-server", 20)),
        {"primary-server": primary, "fallback-server": fallback},
    )
    await registry.discover()

    def require_valid(value: dict) -> dict:
        if not str(value.get("result", "")).startswith("valid:"):
            raise ValueError("unexpected result schema")
        return value

    result = await CapabilityRouter(registry).call(
        "test.echo", {"value": "ok"}, result_adapter=require_valid
    )

    assert result == {"result": "valid:ok"}
    assert registry.last_routes["test.echo"]["fallback_from"] == "primary-server"


@pytest.mark.asyncio
async def test_invalid_input_does_not_fallback() -> None:
    calls = 0
    first = FastMCP("first")
    second = FastMCP("second")

    @first.tool(name="echo")
    def first_echo(value: str) -> str:
        return value

    @second.tool(name="echo")
    def second_echo(value: str) -> str:
        nonlocal calls
        calls += 1
        return value

    registry = MCPRegistry(
        _config(_server("first-server", 10), _server("second-server", 20)),
        {"first-server": first, "second-server": second},
    )
    await registry.discover()
    with pytest.raises(MCPContractError):
        await CapabilityRouter(registry).call("test.echo", {})
    assert calls == 0


@pytest.mark.asyncio
async def test_unhealthy_preferred_server_is_reported_as_degraded_fallback() -> None:
    fallback = FastMCP("fallback")

    @fallback.tool
    def echo(value: str) -> str:
        return value

    registry = MCPRegistry(
        _config(_server("missing-server", 10), _server("fallback-server", 20)),
        {"fallback-server": fallback},
    )
    await registry.discover()
    status = registry.public_status()
    assert status["status"] == "degraded"
    assert status["routes"]["test.echo"]["active_server"] == "fallback-server"
    assert status["routes"]["test.echo"]["using_fallback"] is True
