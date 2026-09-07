"""Probe an OAuth-protected ResearchPilot MCP server as a real MCP client.

Runs the full authorization-code + PKCE flow (opens the browser once) against
a server started with ``--auth github`` and prints the discovered tools.

Usage (from the project root, inside the project venv):

    python -m scripts.run_oauth_probe --url http://localhost:8102/mcp
"""

from __future__ import annotations

import argparse
import asyncio

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def _probe(url: str) -> None:
    async with Client(StreamableHttpTransport(url=url), auth="oauth") as client:
        tools = await client.list_tools()
        print(f"OAuth OK. Server: {url}")
        print("tools:")
        for tool in tools:
            print(f"- {tool.name}")


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="run-oauth-probe",
        description="Run the MCP OAuth flow against a protected server and list tools.",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8102/mcp",
        help="MCP endpoint URL of the OAuth-protected server",
    )
    args = parser.parse_args()
    asyncio.run(_probe(args.url))


if __name__ == "__main__":
    run()
