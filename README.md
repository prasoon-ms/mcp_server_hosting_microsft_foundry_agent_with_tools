# agent-as-tool

A minimal Python project that wraps a **Microsoft Foundry agent** (with an attached **web-search** tool) as an **MCP server**, plus an **MCP client** that drives it over stdio.

```
┌──────────────┐  MCP (stdio JSON-RPC)  ┌──────────────┐  Foundry Agents API   ┌────────────────────┐
│ mcp_client.py│ ─────────────────────► │ mcp_server.py│ ─────────────────────► │ Foundry hosted     │
│              │ ◄───────────────────── │ (FastMCP)    │ ◄───────────────────── │ agent + web_search │
└──────────────┘   tools/list, call     └──────────────┘   OpenAI Responses API └────────────────────┘
```

The hosted Foundry agent owns the web-search tool. The MCP server exposes one tool, `web_search(query)`, that delegates to the agent and returns its grounded answer.

---

## Requirements

- Python 3.11+ (tested on 3.13)
- A Microsoft Foundry project containing an agent with a `web_search` (or equivalent grounding) tool already attached
- Azure credentials usable by `DefaultAzureCredential` (e.g. `az login`)

## Project layout

| File | Purpose |
|------|---------|
| [mcp_server.py](mcp_server.py) | MCP server (stdio). Exposes the `web_search` tool that calls the Foundry agent via the project's OpenAI-compatible endpoint. |
| [mcp_client.py](mcp_client.py) | MCP client. Spawns the server as a subprocess, lists its tools, and calls `web_search`. |
| [requirements.txt](requirements.txt) | Python dependencies. |
| [.env.example](.env.example) | Template for required environment variables. |

## Setup

```powershell
# 1. Create and activate a venv
python -m venv .venv
.\.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
copy .env.example .env
# then edit .env (see below)

# 4. Sign in to Azure so DefaultAzureCredential can get a token
az login
```

### `.env`

```ini
FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
AGENT_NAME=web-search-tool
LOG_LEVEL=DEBUG
```

- `FOUNDRY_ENDPOINT` — full project endpoint URL from the Foundry portal.
- `AGENT_NAME` — name of an agent that already exists in that project and has a web-search tool attached.
- `LOG_LEVEL` — `INFO` for quiet runs, `DEBUG` to stream every MCP frame and every Azure/OpenAI HTTP request.

## Run

```powershell
.\.venv\Scripts\python.exe .\mcp_client.py "latest news on Azure AI Foundry"
```

The client will:

1. Spawn `mcp_server.py` over stdio using the same venv interpreter.
2. Call `initialize` and `tools/list` — printing the tools advertised by the server.
3. Call `tools/call` for `web_search` with your query.
4. Print the agent's response (typically includes inline citations).

If you omit a query, a default demo query is used.

## How it works

### Server (`mcp_server.py`)

1. Loads `.env` and configures logging. With `LOG_LEVEL=DEBUG`, raises log levels for `azure`, `azure.identity`, `azure.ai.projects`, `openai`, `httpx`, `httpcore`, and `mcp` so you can see every downstream HTTP call and every MCP frame. Also sets `OPENAI_LOG=debug` so the OpenAI SDK emits request/response bodies.
2. Lazily creates `AIProjectClient(endpoint=FOUNDRY_ENDPOINT, credential=DefaultAzureCredential(), allow_preview=True, logging_enable=VERBOSE)`. `allow_preview=True` is required for `get_openai_client(agent_name=...)`.
3. Calls `project.get_openai_client(agent_name=AGENT_NAME)`. The returned `openai.OpenAI` client has its `base_url` set to the agent's protocol endpoint (`.../agents/{name}/endpoint/protocols/openai`) and uses a bearer-token provider against `https://ai.azure.com/.default`.
4. Resolves the agent's configured model from `project.agents.get(AGENT_NAME).versions.latest.definition.model` and caches it. The agent endpoint rejects requests where `model` doesn't exactly match this value.
5. Defines a single MCP tool, `web_search(query)`, via `FastMCP`. Each invocation calls `client.responses.create(model=<resolved>, input=query)` — the agent's attached web-search tool fires server-side automatically — then returns `response.output_text`.
6. Logs to **stderr only**; stdout is reserved for the MCP JSON-RPC stream.

### Client (`mcp_client.py`)

1. Loads `.env` and (when `LOG_LEVEL=DEBUG`) raises log levels for `mcp`, `mcp.client.stdio`, and `anyio`.
2. Builds `StdioServerParameters(command=sys.executable, args=["mcp_server.py"], env=os.environ.copy())` so the spawned server inherits the same venv and the same `FOUNDRY_ENDPOINT` / `AGENT_NAME` / Azure credential env vars.
3. Opens an MCP `ClientSession` over stdio, calls `initialize`, `list_tools`, then `call_tool("web_search", {"query": ...})`, and prints the text content from the result.

## Verbose output

With `LOG_LEVEL=DEBUG`, a successful run shows roughly:

```
[INFO]  foundry-mcp-client: Spawning MCP server: ...\python.exe ['...\mcp_server.py']
[DEBUG] foundry-mcp-client: --> session.initialize()
[DEBUG] mcp.server.lowlevel.server: Initializing server 'foundry-web-search'
[DEBUG] foundry-mcp-client: <-- initialize: server=name='foundry-web-search' protocol=2025-11-25 ...
[DEBUG] foundry-mcp-client: --> tools/list
[DEBUG] foundry-mcp-client: <-- tools/list: 1 tool(s)
[DEBUG] foundry-mcp-client: --> tools/call name=web_search args={'query': '...'}
[INFO]  foundry-mcp-server: web_search called: ...
[DEBUG] azure.ai.projects._patch: [get_openai_client] Creating OpenAI client ... base_url = `.../agents/web-search-tool/endpoint/protocols/openai`
[DEBUG] foundry-mcp-server: --> responses.create model=gpt-... query='...'
[DEBUG] foundry-mcp-server: <-- responses.create id=resp_... status=completed usage=...
[DEBUG] foundry-mcp-client: <-- tools/call isError=False content_items=1
```

To capture a full trace to a file:

```powershell
.\.venv\Scripts\python.exe .\mcp_client.py "your query" 2> trace.log
```

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `FOUNDRY_ENDPOINT is not set` at startup | `.env` not present or not loaded; ensure you ran from the project root and `.env` has the variable. |
| `DefaultAzureCredential failed to retrieve a token` | Run `az login`, or set `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` env vars. |
| `400 invalid_payload: Model must match the agent's model '<name>'` | The agent's configured model changed; the server resolves it from `agents.get(...)` and caches it for the process lifetime — restart the server to re-resolve. |
| `Agent named 'X' not found` / `Could not determine model` | Confirm `AGENT_NAME` matches an agent that exists in the project and has at least one published version. |
| `AttributeError: 'AgentsOperations' object has no attribute 'list_agents'` | You're on `azure-ai-projects` 2.x; this project uses the v2.x API path (`get_openai_client` + Responses API), not the v1.x threads/messages/runs API. Reinstall from `requirements.txt`. |
| Long `azure.identity` tracebacks at DEBUG | Normal — `DefaultAzureCredential` probes Environment → ManagedIdentity → AzureCLI etc. in sequence and logs each unavailable step. The call still succeeds via the next credential in the chain. |

## Extending

- **Add more tools** — define more `@mcp.tool()` functions in `mcp_server.py`. Each function's docstring becomes the tool description visible to MCP clients.
- **Use a different agent** — point `AGENT_NAME` at any agent in the project. Tools attached to that agent (web search, file search, code interpreter, custom OpenAPI tools, etc.) all work transparently.
- **Connect from a different host** — any MCP client can drive the server (e.g. Claude Desktop, VS Code's MCP support). Configure it to launch `python mcp_server.py` with `FOUNDRY_ENDPOINT` and `AGENT_NAME` in its environment.

## Dependencies

From [requirements.txt](requirements.txt):

- `mcp` — Model Context Protocol Python SDK (server + client)
- `azure-ai-projects` — Foundry project client (v2.x)
- `azure-identity` — `DefaultAzureCredential`
- `python-dotenv` — `.env` loader
- `openai` — pulled transitively; used for the agent's OpenAI-compatible endpoint
