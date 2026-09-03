import asyncio
import json
import os
import time
from collections import deque
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from app.mcp.models import (
    MCPContractError,
    MCPGatewayConfig,
    MCPServerConfig,
    MCPServerStatus,
    MCPTemporaryError,
)


class MCPRegistry:
    def __init__(
        self,
        config: MCPGatewayConfig,
        inprocess_servers: dict[str, FastMCP] | None = None,
    ) -> None:
        self.config = config
        self.inprocess_servers = inprocess_servers or {}
        self.statuses = {item.id: MCPServerStatus.pending(item) for item in config.servers}
        self.last_routes: dict[str, dict[str, Any]] = {}
        self.invocations: deque[dict[str, Any]] = deque(maxlen=200)
        self.tool_schemas: dict[tuple[str, str], dict[str, Any]] = {}
        # FastMCP in-process servers own lifespan-scoped clients. Avoid opening two
        # lifespans against the same server at once when arXiv and multisource
        # branches are searched concurrently.
        self._server_locks = {item.id: asyncio.Lock() for item in config.servers}

    @classmethod
    def load(
        cls, path: str | Path, inprocess_servers: dict[str, FastMCP] | None = None
    ) -> "MCPRegistry":
        configured = Path(path)
        content = (
            configured.read_text(encoding="utf-8")
            if configured.is_file()
            else files("app.mcp").joinpath("default_servers.json").read_text(encoding="utf-8")
        )
        payload = json.loads(content)
        return cls(MCPGatewayConfig.model_validate(payload), inprocess_servers)

    def transport(self, server: MCPServerConfig):
        if server.transport == "inprocess":
            try:
                return self.inprocess_servers[server.id]
            except KeyError as exc:
                raise MCPContractError(f"No in-process server registered for {server.id}") from exc
        if server.transport == "stdio":
            env = {name: os.environ[name] for name in server.env_allowlist if name in os.environ}
            return StdioTransport(server.command or "", server.args, env=env or None)
        url = str(server.url)
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MCPContractError("Remote HTTP MCP endpoints must use HTTPS")
        auth = os.getenv(server.auth_env) if server.auth_env else None
        return StreamableHttpTransport(url, auth=auth)

    async def discover(self) -> None:
        for server in self.config.servers:
            if not server.enabled:
                continue
            try:
                async with Client(self.transport(server), timeout=server.timeout_seconds) as client:
                    tools = await client.list_tools()
                names = sorted(tool.name for tool in tools)
                invalid = sorted(
                    tool.name for tool in tools
                    if not isinstance(getattr(tool, "inputSchema", None), dict)
                )
                if invalid:
                    raise MCPContractError(f"Invalid input schema: {', '.join(invalid)}")
                missing = sorted(
                    binding.tool for binding in server.capabilities.values()
                    if binding.tool not in names
                )
                if missing:
                    raise MCPContractError(f"Configured tools not discovered: {', '.join(missing)}")
                for tool in tools:
                    self.tool_schemas[(server.id, tool.name)] = tool.inputSchema
                self.statuses[server.id] = self.statuses[server.id].model_copy(update={
                    "healthy": True, "discovered_tools": names, "last_error": None,
                }).checked()
            except Exception as exc:  # noqa: BLE001 - discovery reports optional servers
                self.statuses[server.id] = self.statuses[server.id].model_copy(update={
                    "healthy": False,
                    "last_error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }).checked()

    def candidates(self, capability: str) -> list[tuple[MCPServerConfig, str]]:
        values = []
        for order, server in enumerate(self.config.servers):
            binding = server.capabilities.get(capability)
            status = self.statuses[server.id]
            if server.enabled and status.healthy and binding:
                values.append((binding.priority, order, server, binding.tool))
        return [(server, tool) for _, _, server, tool in sorted(values)]

    async def invoke(self, server: MCPServerConfig, tool: str, arguments: dict[str, Any]) -> Any:
        try:
            self._validate_arguments(server.id, tool, arguments)
            async with (
                self._server_locks[server.id],
                Client(self.transport(server), timeout=server.timeout_seconds) as client,
            ):
                result = await client.call_tool(tool, arguments, timeout=server.timeout_seconds)
            if result.structured_content is not None:
                return result.structured_content
            return [
                block.model_dump(mode="json") if hasattr(block, "model_dump") else str(block)
                for block in result.content
            ]
        except (MCPContractError, ValueError):
            raise
        except Exception as exc:
            self.statuses[server.id] = self.statuses[server.id].model_copy(update={
                "last_error": f"{type(exc).__name__}: {str(exc)[:500]}"
            }).checked()
            raise MCPTemporaryError(f"{server.id}.{tool} failed: {exc}") from exc

    def _validate_arguments(self, server_id: str, tool: str, arguments: dict[str, Any]) -> None:
        schema = self.tool_schemas.get((server_id, tool), {})
        missing = set(schema.get("required", [])) - arguments.keys()
        if missing:
            raise MCPContractError(f"Missing required tool arguments: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        expected_types = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict,
        }
        for name, value in arguments.items():
            expected = properties.get(name, {}).get("type")
            python_type = expected_types.get(expected)
            if python_type and value is not None and not isinstance(value, python_type):
                raise MCPContractError(f"Invalid type for tool argument: {name}")

    def public_status(self) -> dict[str, Any]:
        routes = {}
        for capability in sorted({
            name for server in self.config.servers for name in server.capabilities
        }):
            candidates = self.candidates(capability)
            configured = [
                (server.capabilities[capability].priority, index, server)
                for index, server in enumerate(self.config.servers)
                if server.enabled and capability in server.capabilities
            ]
            preferred = min(configured, default=None, key=lambda item: (item[0], item[1]))
            routes[capability] = {
                "active_server": candidates[0][0].id if candidates else None,
                "fallback_servers": [server.id for server, _ in candidates[1:]],
                "using_fallback": bool(
                    candidates and preferred and preferred[2].id != candidates[0][0].id
                ),
            }
        return {
            "status": "ready" if all(
                self.statuses[server.id].healthy
                for server in self.config.servers if server.enabled
            ) else "degraded",
            "servers": [value.model_dump(mode="json") for value in self.statuses.values()],
            "routes": routes,
            "last_routes": self.last_routes,
            "recent_invocations": list(self.invocations),
        }


class CapabilityRouter:
    def __init__(self, registry: MCPRegistry) -> None:
        self.registry = registry

    async def call(
        self,
        capability: str,
        arguments: dict[str, Any],
        arguments_by_server: dict[str, dict[str, Any]] | None = None,
        context: dict[str, str | None] | None = None,
        result_adapter=None,
    ) -> Any:
        candidates = self.registry.candidates(capability)
        if not candidates:
            raise MCPTemporaryError(f"No healthy MCP server provides {capability}")
        failures = []
        for index, (server, tool) in enumerate(candidates):
            started = time.perf_counter()
            try:
                effective = (arguments_by_server or {}).get(server.id, arguments)
                result = await self.registry.invoke(server, tool, effective)
                if result_adapter is not None:
                    try:
                        result = result_adapter(result)
                    except Exception as exc:  # malformed server output is retryable
                        raise MCPTemporaryError(
                            f"{server.id}.{tool} returned an invalid result: {exc}"
                        ) from exc
                event = {
                    "capability": capability,
                    "server_id": server.id,
                    "tool_name": tool,
                    "transport": server.transport,
                    "latency_ms": round((time.perf_counter() - started) * 1_000),
                    "success": True,
                    "fallback_from": failures[0] if failures else None,
                    "fallback_to": server.id if failures else None,
                    "project_id": (context or {}).get("project_id"),
                    "trace_id": (context or {}).get("trace_id"),
                }
                self.registry.last_routes[capability] = event
                self.registry.invocations.append(event)
                return result
            except MCPTemporaryError as exc:
                self.registry.invocations.append({
                    "capability": capability,
                    "server_id": server.id,
                    "tool_name": tool,
                    "transport": server.transport,
                    "latency_ms": round((time.perf_counter() - started) * 1_000),
                    "success": False,
                    "error": type(exc).__name__,
                    "project_id": (context or {}).get("project_id"),
                    "trace_id": (context or {}).get("trace_id"),
                })
                failures.append(server.id)
                if index == len(candidates) - 1:
                    raise
        raise MCPTemporaryError(f"No MCP route completed {capability}")
