import argparse
from pathlib import Path

from dotenv import load_dotenv
from fastmcp.server.auth import TokenVerifier  # noqa: F401  (re-exported for tooling)

from mcp_servers.document import create_document_server
from mcp_servers.literature import create_literature_server
from mcp_servers.oauth import (
    STATIC_TOKEN_ENV,
    StaticBearerTokenVerifier,  # noqa: F401  (kept importable here for back-compat)
    build_auth,
    is_loopback,
)

# Auto-load the project .env (like the FastAPI app does) so OAuth credentials
# can live in .env instead of shell variables. Existing process environment
# takes precedence over .env values (python-dotenv default).
_PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchpilot-mcp")
    parser.add_argument("--server", choices=("literature", "document"), required=True)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument(
        "--auth",
        choices=("none", "static", "github"),
        default=None,
        help=(
            "HTTP auth mode. none: no auth; static: pre-shared Bearer token "
            f"({STATIC_TOKEN_ENV}); github: OAuth 2.1 authorization-code flow "
            "proxied to a GitHub OAuth App (requires MCP_GITHUB_CLIENT_ID / "
            "MCP_GITHUB_CLIENT_SECRET in .env or the environment, plus a public "
            "base URL). Defaults: none on loopback binds, static token on "
            "non-loopback binds."
        ),
    )
    parser.add_argument(
        "--public-base-url",
        default=None,
        help=(
            "Public base URL of this MCP endpoint (required for --auth github "
            "unless MCP_OAUTH_BASE_URL is set in .env, e.g. "
            "https://mcp.example.com). The GitHub OAuth App callback must be "
            "registered as {public-base-url}/auth/callback."
        ),
    )
    return parser


def _resolve_auth(args: argparse.Namespace):
    """Return (auth, description); stdio transport is never authenticated."""
    if args.transport == "stdio":
        return None, "none (stdio)"
    loopback = is_loopback(args.host)
    if args.auth is None and loopback:
        return None, "none (loopback)"
    try:
        return build_auth(args.auth, host=args.host, public_base_url=args.public_base_url)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    load_dotenv(_PROJECT_ENV_FILE)
    args = _parser().parse_args(argv)
    auth, auth_description = _resolve_auth(args)

    if args.transport == "http" and auth is None and not is_loopback(args.host):
        # Guard against accidentally exposing an unauthenticated endpoint.
        raise SystemExit(
            "error: refusing to bind an unauthenticated HTTP MCP endpoint on a "
            "non-loopback host; pass --auth static/--auth github or bind loopback"
        )

    if args.server == "literature":
        server = create_literature_server(auth=auth)
    else:
        server = create_document_server(auth=auth)

    print(
        f"researchpilot-mcp: {args.server} over {args.transport} "
        f"(auth: {auth_description})",
        flush=True,
    )

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
