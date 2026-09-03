import argparse
import asyncio
import hmac
import os
from ipaddress import ip_address

from fastmcp.server.auth import AccessToken, TokenVerifier

from app.artifacts import ArtifactService
from app.core.config import get_settings
from app.db import ArtifactRepository, Database
from app.documents import WorkspaceManager
from mcp_servers.artifact import create_artifact_server
from mcp_servers.document import create_document_server
from mcp_servers.literature import create_literature_server


class StaticBearerTokenVerifier(TokenVerifier):
    """Small single-user verifier for self-hosted non-loopback MCP endpoints."""

    def __init__(self, expected: str, base_url: str) -> None:
        super().__init__(base_url=base_url)
        self.expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self.expected):
            return None
        return AccessToken(token=token, client_id="researchpilot-mcp", scopes=[])


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchpilot-mcp")
    parser.add_argument("--server", choices=("literature", "document", "artifact"), required=True)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    return parser


def _artifact_server(auth: object | None):
    settings = get_settings()
    database = Database(settings.database_path)
    asyncio.run(database.initialize())
    workspace = WorkspaceManager(
        settings.workspace_root, max_document_bytes=settings.document_max_bytes
    )
    return create_artifact_server(ArtifactService(workspace, ArtifactRepository(database)), auth)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    auth = None
    if args.transport == "http" and not _is_loopback(args.host):
        token = os.getenv("MCP_AUTH_TOKEN")
        if not token:
            raise SystemExit("MCP_AUTH_TOKEN is required when binding HTTP outside loopback")
        if len(token) < 24:
            raise SystemExit("MCP_AUTH_TOKEN must contain at least 24 characters")
        auth = StaticBearerTokenVerifier(token, f"http://{args.host}:{args.port}")

    if args.server == "literature":
        server = create_literature_server(auth=auth)
    elif args.server == "document":
        server = create_document_server(allow_local_files=args.transport == "stdio", auth=auth)
    else:
        server = _artifact_server(auth)

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
