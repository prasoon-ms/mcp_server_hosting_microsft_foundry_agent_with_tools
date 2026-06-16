"""
MCP client that spawns `mcp_server.py` over stdio, discovers its tools, and
invokes the `web_search` tool (which is backed by a Microsoft Foundry agent
with an attached web-search tool).

Run:
    python mcp_client.py "your query here"

If no query is given on the command line, a default demo query is used.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("foundry-mcp-client")

# Verbose mode: at DEBUG, also stream MCP protocol frames and the underlying
# anyio/asyncio plumbing so you can see every call between client and server.
VERBOSE = LOG_LEVEL == "DEBUG"
if VERBOSE:
    for name in (
        "mcp",
        "mcp.client",
        "mcp.client.stdio",
        "anyio",
    ):
        logging.getLogger(name).setLevel(logging.DEBUG)

HERE = Path(__file__).resolve().parent
SERVER_SCRIPT = HERE / "mcp_server.py"


def _extract_text(result) -> str:
    """Pull plain text out of a tools/call result."""
    chunks: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


async def run(query: str) -> None:
    # Use the same Python interpreter that's running this client (i.e. the venv).
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env=os.environ.copy(),  # forwards FOUNDRY_ENDPOINT / AGENT_NAME / creds
    )

    log.info("Spawning MCP server: %s %s", server_params.command, server_params.args)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            log.debug("--> session.initialize()")
            init_result = await session.initialize()
            log.debug(
                "<-- initialize: server=%s protocol=%s capabilities=%s",
                getattr(init_result, "serverInfo", None),
                getattr(init_result, "protocolVersion", None),
                getattr(init_result, "capabilities", None),
            )

            log.debug("--> tools/list")
            tools = await session.list_tools()
            log.debug("<-- tools/list: %d tool(s)", len(tools.tools))
            print("Available tools from MCP server:")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description or ''}")
            print()

            print(f"Calling web_search(query={query!r}) ...\n")
            log.debug("--> tools/call name=web_search args=%r", {"query": query})
            result = await session.call_tool("web_search", {"query": query})
            log.debug(
                "<-- tools/call isError=%s content_items=%d",
                getattr(result, "isError", False),
                len(getattr(result, "content", []) or []),
            )

            text = _extract_text(result)
            if getattr(result, "isError", False):
                print("[tool returned an error]")
            print(text or "(no text content returned)")


def main() -> None:
    query = " ".join(sys.argv[1:]).strip() or "What are the top news headlines today?"
    asyncio.run(run(query))


if __name__ == "__main__":
    main()
