"""
MCP server that exposes a Microsoft Foundry agent (with an attached web-search /
grounding tool) as an MCP tool named `web_search`.

The server speaks the standard MCP stdio transport, so any MCP client (including
`mcp_client.py` in this repo) can spawn it as a subprocess and call its tools.

Required env vars (loaded from .env):
    FOUNDRY_ENDPOINT  e.g. https://<project-endpoint-name>.services.ai.azure.com/api/projects/<project-name>
    AGENT_NAME        name of the agent already created in the Foundry project
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# IMPORTANT: log to stderr only. stdout is reserved for the MCP JSON-RPC stream.
logging.basicConfig(
    level=LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("foundry-mcp-server")

# Verbose mode: at DEBUG, also stream Azure SDK, OpenAI, HTTP, and MCP traffic
# so you can see every call the server makes downstream and every MCP frame it
# exchanges with the client.
VERBOSE = LOG_LEVEL == "DEBUG"
if VERBOSE:
    for name in (
        "azure",                  # azure-core pipeline (HTTP requests/responses)
        "azure.identity",         # token acquisition (DefaultAzureCredential chain)
        "azure.ai.projects",      # project client operations
        "openai",                 # OpenAI SDK request/response bodies
        "httpx",                  # HTTP client used by openai
        "httpcore",               # low-level HTTP
        "mcp",                    # MCP protocol frames
        "mcp.server",
    ):
        logging.getLogger(name).setLevel(logging.DEBUG)
    # Tell the OpenAI SDK to emit full request/response logs (incl. bodies).
    os.environ.setdefault("OPENAI_LOG", "debug")

FOUNDRY_ENDPOINT = os.getenv("FOUNDRY_ENDPOINT")
AGENT_NAME = os.getenv("AGENT_NAME")

if not FOUNDRY_ENDPOINT:
    raise RuntimeError("FOUNDRY_ENDPOINT is not set in environment / .env")
if not AGENT_NAME:
    raise RuntimeError("AGENT_NAME is not set in environment / .env")


# --- Foundry client (lazy singleton) -----------------------------------------

_project_client: Optional[AIProjectClient] = None
_openai_client: Optional[OpenAI] = None
_agent_model: Optional[str] = None


def _get_project_client() -> AIProjectClient:
    global _project_client
    if _project_client is None:
        log.debug("Creating AIProjectClient for endpoint=%s", FOUNDRY_ENDPOINT)
        _project_client = AIProjectClient(
            endpoint=FOUNDRY_ENDPOINT,
            credential=DefaultAzureCredential(),
            allow_preview=True,  # required to scope the OpenAI client to an agent
            # Emit full HTTP request/response (headers + bodies) when verbose.
            logging_enable=VERBOSE,
        )
    return _project_client


def _get_openai_client() -> OpenAI:
    """Return an OpenAI client whose base_url points at the Foundry agent's
    endpoint (`.../agents/{name}/endpoint/protocols/openai`)."""
    global _openai_client
    if _openai_client is None:
        project = _get_project_client()
        log.debug("Resolving OpenAI client for agent=%s", AGENT_NAME)
        _openai_client = project.get_openai_client(agent_name=AGENT_NAME)
    return _openai_client


def _get_agent_model() -> str:
    """Look up the agent's configured model. The agent endpoint enforces that
    the `model` parameter on each call matches this value exactly."""
    global _agent_model
    if _agent_model is not None:
        return _agent_model

    project = _get_project_client()
    details = project.agents.get(AGENT_NAME)
    versions = details.get("versions") or {}
    latest = versions.get("latest") or {}
    model = (latest.get("definition") or {}).get("model")
    if not model:
        raise RuntimeError(
            f"Could not determine model for agent '{AGENT_NAME}' from agent details"
        )
    _agent_model = model
    log.info("Resolved agent '%s' model -> %s", AGENT_NAME, _agent_model)
    return _agent_model


def _invoke_foundry_agent(query: str) -> str:
    """Send `query` to the Foundry agent (via its OpenAI-compatible endpoint)
    and return the assistant's text reply. The agent's attached web-search tool
    is invoked server-side automatically when the model decides to use it."""
    client = _get_openai_client()
    model = _get_agent_model()

    log.debug("--> responses.create model=%s query=%r", model, query)
    response = client.responses.create(
        model=model,
        input=query,
    )
    log.debug(
        "<-- responses.create id=%s status=%s usage=%s",
        getattr(response, "id", "?"),
        getattr(response, "status", "?"),
        getattr(response, "usage", None),
    )

    text = (getattr(response, "output_text", None) or "").strip()
    return text or "(agent returned no text)"


# --- MCP server --------------------------------------------------------------

mcp = FastMCP("foundry-web-search")


@mcp.tool()
def web_search(query: str) -> str:
    """Search the web by delegating to a Microsoft Foundry agent that has a
    web-search / grounding tool attached. Returns the agent's synthesized answer
    (which typically includes citations).

    Args:
        query: Natural-language search query, e.g. "latest news on Azure AI Foundry".
    """
    log.info("web_search called: %s", query)
    try:
        return _invoke_foundry_agent(query)
    except Exception as e:  # surface the error back to the MCP client
        log.exception("web_search failed")
        return f"ERROR: {type(e).__name__}: {e}"


if __name__ == "__main__":
    log.info("Starting MCP server (stdio) for agent=%s", AGENT_NAME)
    mcp.run()
