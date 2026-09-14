from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

TransportKind = Literal["inprocess", "stdio", "http"]


class MCPToolBinding(BaseModel):
    tool: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0, le=10_000)


class MCPServerConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    source: Literal["researchpilot", "third_party"]
    transport: TransportKind
    enabled: bool = True
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: HttpUrl | None = None
    auth_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    env_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    capabilities: dict[str, MCPToolBinding]

    @field_validator("env_allowlist")
    @classmethod
    def environment_names_only(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or not value.replace("_", "A").isalnum() or value.upper() != value:
                raise ValueError("env_allowlist accepts uppercase environment variable names only")
        return values

    @model_validator(mode="after")
    def validate_transport(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP servers require command")
        if self.transport == "http" and self.url is None:
            raise ValueError("http MCP servers require url")
        if self.transport != "stdio" and (self.command or self.args):
            raise ValueError("command and args are only valid for stdio")
        if self.transport != "http" and (self.url or self.auth_env):
            raise ValueError("url and auth_env are only valid for http")
        if not self.capabilities:
            raise ValueError("MCP servers require at least one mapped capability")
        return self


class MCPGatewayConfig(BaseModel):
    version: Literal[1]
    servers: list[MCPServerConfig]

    @model_validator(mode="after")
    def unique_servers(self) -> "MCPGatewayConfig":
        ids = [server.id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server ids must be unique")
        return self


class MCPServerStatus(BaseModel):
    """Runtime health of one configured server.

    Static identity (id/source/transport/enabled) lives on ``MCPServerConfig``;
    this card only tracks what discovery/invocation observed at runtime.
    """

    id: str
    healthy: bool = False
    discovered_tools: list[str] = Field(default_factory=list)
    last_error: str | None = None
    checked_at: str | None = None

    @classmethod
    def pending(cls, server_id: str) -> "MCPServerStatus":
        return cls(id=server_id)

    def checked(self) -> "MCPServerStatus":
        return self.model_copy(update={"checked_at": datetime.now(UTC).isoformat()})


class MCPContractError(ValueError):
    """A configured capability does not match the discovered MCP contract."""


class MCPTemporaryError(RuntimeError):
    """An MCP transport or server failed and an explicit fallback may be used."""
