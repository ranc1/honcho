# MCP Internal Tools Migration — Bugfix Design

## Overview

The Python MCP server (`src/mcp_server.py`) was modeled after Honcho's upstream TypeScript MCP (designed for external API consumers) instead of Honcho's internal tool set (`src/utils/agent_tools.py`). This causes name mismatches, schema mismatches, missing tools, and wrong-collection writes when the ACP engine calls tools for dreamer/dialectic modules.

The fix has three parts:

1. **MCP tool wrapping**: Replace upstream-MCP-style tool definitions and HTTP-based dispatch with internal tool names and schemas. Remove `TOOL_NAME_MAP` from `acp_provider.py`.
2. **Per-module tool executor via HTTP context registration**: Expose 3 per-module MCP endpoints (`/mcp/dreamer`, `/mcp/dialectic`, `/mcp/deriver`). Before each ACP call, the calling process (deriver or FastAPI) registers the tool execution context with the MCP server via HTTP. The MCP server creates a live `tool_executor` closure in its own process from the received parameters. Works cross-process (deriver → FastAPI) and same-process (dialectic in FastAPI).
3. **Client-facing MCP restoration**: Restore the upstream TypeScript MCP as a separate Node.js process for external consumers.

## Implementation Constraint

All changes MUST be confined to fork-only files (`src/mcp_server.py`, `src/utils/acp_provider.py`) except for two minimal core code changes: (1) threading the existing `tool_executor` param through `honcho_llm_call_inner()` in `clients.py`, and (2) attaching `_ctx` to the closure in `create_tool_executor()` in `agent_tools.py` (1 line, no signature changes). No changes to `specialists.py`, `dialectic/core.py`, or any other core Honcho code.

## Part 1: MCP Tool Wrapping

### Tool Portability Analysis

| # | Tool Name | Port Strategy | ToolContext Fields Used |
|---|-----------|--------------|----------------------|
| 1 | `create_observations` | 100% direct dispatch | `observer`, `observed`, `workspace_name`, `session_name`, `current_messages` (None), `db_lock` |
| 2 | `update_peer_card` | 100% direct dispatch | `observer`, `observed`, `workspace_name`, `configuration` (None), `db_lock` |
| 3 | `search_memory` | 100% direct dispatch | `workspace_name`, `observer`, `observed`, `session_name`, `include_observation_ids`, `agent_type` |
| 4 | `get_observation_context` | 100% direct dispatch | `workspace_name`, `session_name` |
| 5 | `search_messages` | 100% direct dispatch | `workspace_name`, `session_name` |
| 6 | `grep_messages` | 100% direct dispatch | `workspace_name`, `session_name` |
| 7 | `get_messages_by_date_range` | 100% direct dispatch | `workspace_name`, `session_name` |
| 8 | `search_messages_temporal` | 100% direct dispatch | `workspace_name`, `session_name` |
| 9 | `get_recent_observations` | 100% direct dispatch | `workspace_name`, `observer`, `observed`, `session_name`, `include_observation_ids` |
| 10 | `get_most_derived_observations` | 100% direct dispatch | `workspace_name`, `observer`, `observed`, `include_observation_ids` |
| 11 | `get_peer_card` | 100% direct dispatch | `workspace_name`, `observer`, `observed` |
| 12 | `delete_observations` | 100% direct dispatch | `workspace_name`, `observer`, `observed`, `db_lock` |
| 13 | `finish_consolidation` | 100% direct dispatch | (none) |
| 14 | `extract_preferences` | 100% direct dispatch | `workspace_name`, `session_name`, `observed` |
| 15 | `get_reasoning_chain` | 100% direct dispatch | `workspace_name`, `observer`, `observed` |
| 16 | `honcho_extract_facts` | Custom logic (store) | N/A — module-level extraction store |

15 tools dispatch directly to `_TOOL_HANDLERS`. `honcho_extract_facts` keeps its custom extraction store logic. `chat` is removed — it's not an internal tool and shouldn't be exposed to the engine.

### MCP_TOOL_DEFS Auto-Generation

Replace hand-written `MCP_TOOL_DEFS` with auto-generated definitions from the `TOOLS` dict:

```python
from src.utils.agent_tools import TOOLS, _TOOL_HANDLERS

_INTERNAL_TOOL_NAMES = [
    "create_observations", "update_peer_card", "search_memory",
    "get_observation_context", "search_messages", "grep_messages",
    "get_messages_by_date_range", "search_messages_temporal",
    "get_recent_observations", "get_most_derived_observations",
    "get_peer_card", "delete_observations", "finish_consolidation",
    "extract_preferences", "get_reasoning_chain",
]

# Startup assertion: fail fast if upstream removes a tool
for _name in _INTERNAL_TOOL_NAMES:
    assert _name in TOOLS, f"Internal tool '{_name}' not found in TOOLS dict"
    assert _name in _TOOL_HANDLERS, f"Internal tool '{_name}' not found in _TOOL_HANDLERS"

MCP_TOOL_DEFS = [
    {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
    for name in _INTERNAL_TOOL_NAMES
    for t in [TOOLS[name]]
]
MCP_TOOL_DEFS.append({...honcho_extract_facts def...})
```

### dispatch_tool() — Part 1 Schema

The tool dispatch logic is defined in Part 2. Part 1 only covers the tool definitions (`MCP_TOOL_DEFS`) and schema matching. See Part 2 for the full `dispatch_tool()` implementation using stored `tool_executor`.

### acp_provider.py Changes

- Delete `TOOL_NAME_MAP` entirely
- In `build_agentic_prompt()`, replace `mcp_name = TOOL_NAME_MAP.get(name, name)` with just `name`

### Coupling Note

`MCP_TOOL_DEFS` is auto-generated from `TOOLS` at import time, so schemas are guaranteed to match. If upstream Honcho changes a tool in `TOOLS` or `_TOOL_HANDLERS`, the `_INTERNAL_TOOL_NAMES` list must be updated. The startup assertions catch mismatches at import time.

### Removed Tools

- `chat` — REST API endpoint, not in `_TOOL_HANDLERS`, not in any specialist tool list. Engine discovered it from `tools/list` and called it with guessed `peer_id` values.
- `get_peer_context` — Not in `_TOOL_HANDLERS`, not used by any module.
- `list_conclusions`, `query_conclusions`, `create_conclusions`, `delete_conclusion`, `set_peer_card` — Upstream MCP names replaced by internal names.


## Part 2: Per-Module Tool Executor via HTTP Context Registration

### The Problem

In the original Honcho, `create_tool_executor()` returns a closure that captures observer/observed in a `ToolContext` and is invoked by `_execute_tool_loop`. The ACP path bypasses `_execute_tool_loop` — the engine calls MCP tools directly. The MCP server needs the execution context but has no way to get it.

The dreamer runs in the deriver process (`python -m src.deriver`), while the MCP server runs in the FastAPI process. Python closures and module-level dicts can't be shared across processes. The solution must use HTTP to bridge the process boundary.

### Why Per-Module Endpoints

The MCP protocol's `tools/call` request contains only `name` and `arguments` — no session ID, no caller context. If dreamer and dialectic run concurrently with different observed peers, a single endpoint can't distinguish which module sent the request. The URL path is the only mechanism to identify the caller.

### Solution: HTTP Context Registration

Before each ACP call, the calling process (deriver or FastAPI) registers the tool execution context with the MCP server via HTTP. The MCP server creates a live `tool_executor` closure in its own process from the received parameters.

3 identical per-module MCP endpoints:

| Module | Endpoint | Tool Calls |
|---|---|---|
| `dreamer` | `POST /mcp/dreamer` | Internal tools + `honcho_extract_facts` |
| `dialectic` | `POST /mcp/dialectic` | Internal tools + `honcho_extract_facts` |
| `deriver` | `POST /mcp/deriver` | `honcho_extract_facts` only (in practice) |
| `summarizer` | (none) | No tool calls |

All 3 endpoints expose the same 16 tools with the same dispatch logic. No special cases per module.

### Data Flow

```
1. Any process (deriver or FastAPI): specialist.run(observer, observed)
   → honcho_llm_call_inner_acp(tool_executor=tool_executor, ...)
   → POST http://localhost:8000/mcp/{module}/context
     body: {workspace_name, observer, observed, session_name,
            include_observation_ids, history_token_limit}
   → MCP server (FastAPI process) receives the params
   → Calls create_tool_executor(workspace_name, observer, observed, ...)
   → Stores live tool_executor in _module_tool_executor[module]
   → Returns 200 OK

2. Calling process continues:
   → POST to ACP gateway (awaits response)

3. Engine processes prompt, decides to call tools
   → POST /mcp/{module} (tools/call: search_memory)

4. MCP server (FastAPI process):
   → dispatch_tool("search_memory", args, module)
   → tool_executor = _module_tool_executor[module]  ← live closure in FastAPI process
   → await tool_executor("search_memory", args)
   → Returns result to engine

5. Engine may call more tools (repeat 3-4)

6. Calling process resumes after gateway returns
   → DELETE http://localhost:8000/mcp/{module}/context
   → MCP server removes _module_tool_executor[module]
```

### Why This Works

- **Cross-process safe**: Step 1 sends serializable params (strings) via HTTP. The MCP server creates the `tool_executor` in its own process. Steps 3-4 use it in the same process. No shared memory needed.
- **Same-process also works**: The dialectic runs in the FastAPI process. It can use the same HTTP endpoint (uniform code path) or set `_module_tool_executor` directly. Using HTTP for both is simpler — one code path.
- **Concurrency**: The ACP gateway serializes per-module (concurrency-1 FIFO). Different modules use different dict keys and different endpoints.
- **Cleanup**: Step 6 always runs (in a `finally` block), even on timeout/error.

### Context Registration Endpoint

```python
@router.post("/mcp/{module}/context")
async def register_context(request: Request, module: str) -> JSONResponse:
    """Register tool execution context for a module before ACP call."""
    if module not in ("dreamer", "dialectic", "deriver"):
        return JSONResponse(status_code=404, content={"error": f"Unknown module: {module}"})
    body = await request.json()
    tool_executor = await create_tool_executor(
        workspace_name=body["workspace_name"],
        observer=body["observer"],
        observed=body["observed"],
        session_name=body.get("session_name"),
        include_observation_ids=body.get("include_observation_ids", True),
        history_token_limit=body.get("history_token_limit", 10000),
    )
    _module_tool_executor[module] = tool_executor
    return JSONResponse({"status": "ok"})

@router.delete("/mcp/{module}/context")
async def deregister_context(request: Request, module: str) -> JSONResponse:
    """Remove tool execution context after ACP call completes."""
    _module_tool_executor.pop(module, None)
    return JSONResponse({"status": "ok"})
```

The MCP server calls `create_tool_executor()` with the received params. This reuses all existing logic — `db_lock` acquisition via `get_observation_lock()`, `ToolContext` construction, error handling, and telemetry. No replication of handler dispatch logic in the MCP server.

### ACP Provider — Context Registration via HTTP

In `honcho_llm_call_inner_acp()`, the ACP provider reads the `ToolContext` from the `tool_executor` closure (via `tool_executor._ctx`, a one-line core change in `create_tool_executor`) and sends the serializable fields to the MCP server:

```python
module = detect_module(messages, tools)

if tool_executor is not None:
    ctx = tool_executor._ctx  # ToolContext attached to closure
    await _register_mcp_context(module, {
        "workspace_name": ctx.workspace_name,
        "observer": ctx.observer,
        "observed": ctx.observed,
        "session_name": ctx.session_name,
        "include_observation_ids": ctx.include_observation_ids,
        "history_token_limit": ctx.history_token_limit,
    })

try:
    response = await client.post(bridge_url, ...)
finally:
    if tool_executor is not None:
        await _deregister_mcp_context(module)
```

The `_register_mcp_context` helper POSTs to `http://localhost:8000/mcp/{module}/context`. The `_deregister_mcp_context` helper sends DELETE. Both are simple `httpx` calls.

### dispatch_tool() — Uses Stored Tool Executor

```python
async def dispatch_tool(name: str, args: dict, module: str) -> dict:
    if name in _TOOL_HANDLERS:
        tool_executor = _module_tool_executor.get(module)
        if tool_executor is None:
            return _error(f"No tool executor for module '{module}'")
        result = await tool_executor(name, args)
        return _text(result)
    elif name == "honcho_extract_facts":
        # extraction store logic
        ...
    else:
        return _error(f"Unknown tool: {name}")
```

The MCP server doesn't know about `ToolContext` construction details. It just calls the closure that was created during context registration.

### Tool Call Limit Enforcement (Future Enhancement)

**Not yet implemented.** Documented here for future reference.

The MCP server tracks tool call count per module and returns a stop message when the limit is exceeded. This mirrors the `max_tool_iterations` limits in Honcho's specialist classes:

| Module | Honcho Limit | MCP Limit |
|---|---|---|
| `dreamer` (deduction) | 12 | 12 |
| `dreamer` (induction) | 10 | 10 |
| `dialectic` | varies by reasoning level | 15 (conservative default) |

The call counter is stored alongside the tool_executor in `_module_tool_executor` and reset on context deregistration. When exceeded, `dispatch_tool()` returns:

```
TOOL_LIMIT_REACHED: You have exceeded the maximum number of tool calls ({limit}).
Stop calling tools and finish your task now. Call finish_consolidation if you are
a dreamer specialist.
```

This is a soft limit — the engine may ignore it. But LLMs generally respect explicit stop instructions in tool results. The context registration endpoint accepts an optional `max_tool_calls` parameter so the caller can set the limit per-module.

### Core Code Changes (Minimal)

Two changes to core code:

1. **`clients.py`** — Thread the existing `tool_executor` param through `honcho_llm_call_inner()`:
   - Add `tool_executor: Callable | None = None` to `honcho_llm_call_inner()` (all overloads + implementation) and `handle_streaming_response()`
   - In `honcho_llm_call`'s `_call_with_provider_selection`, pass `tool_executor=tool_executor` to `honcho_llm_call_inner()`
   - In the ACP branch of `honcho_llm_call_inner()`, pass `tool_executor=tool_executor` to `honcho_llm_call_inner_acp()`

2. **`agent_tools.py`** — Attach `ToolContext` to the closure (1 line):
   ```python
   # In create_tool_executor(), before return:
   execute_tool._ctx = ctx  # type: ignore[attr-defined]
   return execute_tool
   ```
   This lets the ACP provider read `tool_executor._ctx.observer`, etc. to send via HTTP to the MCP server. No function signature changes.

No changes to `specialists.py` or `dialectic/core.py`.

### MCP Endpoint Routes

```python
@router.post("/mcp/{module}")
async def mcp_module_endpoint(request: Request, module: str) -> JSONResponse:
    if module not in ("dreamer", "dialectic", "deriver"):
        return JSONResponse(status_code=404, ...)
    # Same JSON-RPC handling, pass module to dispatch_tool()
```

The existing `POST /mcp` endpoint on port 8000 is removed for tool dispatch. Port 8000 retains `GET /mcp/health`, `GET /mcp/extraction`, `POST /mcp/{module}` (JSON-RPC), `POST /mcp/{module}/context`, and `DELETE /mcp/{module}/context`. The client-facing TS MCP on port 8001 handles `POST /mcp` for external consumers.

**Client-side configuration (documented separately, not part of this spec):**
The ACP gateway creates per-module sessions and registers per-module MCP server URLs by appending `/{module}` to the base MCP URL.

## Part 3: Client-Facing MCP (honcho-mcp)

### The Problem

The current Python MCP at `/mcp` serves both internal tools (for dreamer/dialectic) and client-facing tools (for the CLI engine during normal user conversations). Part 1 and 2 replace `/mcp` with per-module internal endpoints and remove client-facing tools (`chat`, `get_peer_context`, `query_conclusions`, etc.). This breaks client access to Honcho's memory.

### Solution: Restore the Upstream TypeScript MCP

Run the original upstream TypeScript MCP as a separate process in the same Docker container. The upstream code (`mcp/` directory on `main` branch) already implements all 24 client-facing tools using `@honcho-ai/sdk` and `@modelcontextprotocol/sdk`. The tool implementations are REST API wrappers — no shared memory or direct DB access needed.

### What Changes

Only the entry point (`mcp/src/index.ts`) changes. The upstream uses Cloudflare Workers (`export default { fetch() }` + `agents/mcp` handler). Replace with a standard Node.js HTTP server using `@modelcontextprotocol/sdk`'s built-in HTTP transport.

**Unchanged (reused as-is):**
- `mcp/src/server.ts` — McpServer creation and tool registration
- `mcp/src/tools/conclusions.ts` — 4 conclusion tools
- `mcp/src/tools/peers.ts` — 8 peer tools (including `chat`, `get_peer_card`, `set_peer_card`, `get_peer_context`, `get_representation`)
- `mcp/src/tools/sessions.ts` — 12 session tools (including `get_session_messages`, `get_session_context`)
- `mcp/src/tools/system.ts` — 2 system tools (`schedule_dream`, `get_queue_status`)
- `mcp/src/tools/workspace.ts` — 6 workspace tools (including `search`)
- `mcp/src/types.ts` — shared types and helpers

**Changed:**
- `mcp/src/index.ts` — replace Cloudflare Workers entry point with minimal Node.js HTTP server using `@modelcontextprotocol/sdk`'s `StreamableHTTPServerTransport`
- `mcp/src/config.ts` — read config from env vars (`MCP_WORKSPACE_ID`, `HONCHO_BASE_URL`) instead of Cloudflare request headers

**Removed:**
- `mcp/wrangler.toml` — Cloudflare-specific config

### Docker Changes

Add Node.js back to the Dockerfile for the client-facing MCP process:

```dockerfile
# Add Node.js stage
FROM node:22-slim AS mcp-build
WORKDIR /mcp
COPY mcp/package.json mcp/bun.lock ./
RUN npm install --production
COPY mcp/src ./src
COPY mcp/tsconfig.json ./
RUN npx tsc

# In final stage, copy built MCP
COPY --from=mcp-build /mcp/dist /app/mcp/dist
COPY --from=mcp-build /mcp/node_modules /app/mcp/node_modules
```

Update the container entrypoint to start both processes:

```sh
alembic upgrade head && \
  node /app/mcp/dist/index.js &
  python -m src.deriver & \
  fastapi run --host 0.0.0.0 --port 8000 src/main.py
```

### Port Assignment

| Process | Port | Purpose |
|---|---|---|
| FastAPI (Honcho API + internal MCP) | 8000 | REST API + `/mcp/{module}` endpoints |
| TS MCP (client-facing) | 8001 | `/mcp` endpoint for external consumers |

### Config

The TS MCP reads config from env vars set in the Docker Compose configuration:

| Variable | Value | Purpose |
|---|---|---|
| `HONCHO_BASE_URL` | `http://localhost:8000` | Honcho REST API (same container) |
| `MCP_WORKSPACE_ID` | (configured per deployment) | Workspace for all operations |
| `HONCHO_API_KEY` | (empty or dummy) | Auth disabled for local |

## Files Changed

| File | Change | Core? |
|---|---|---|
| `src/mcp_server.py` | Rewrite: internal tools, per-module endpoints, context registration, tool_executor dispatch | Fork |
| `src/utils/acp_provider.py` | Remove TOOL_NAME_MAP, add HTTP context registration/deregistration calls | Fork |
| `src/utils/clients.py` | Pass existing tool_executor param through honcho_llm_call_inner() | Core (minimal — threading existing param deeper) |
| `src/utils/agent_tools.py` | Attach `_ctx` to tool_executor closure (1 line) | Core (minimal — no signature changes) |
| `mcp/src/index.ts` | Replace Cloudflare Workers entry point with Node.js HTTP server | Fork (new entry point) |
| `mcp/src/config.ts` | Read config from env vars instead of request headers | Fork (modified) |
| `Dockerfile` | Add Node.js build stage, copy TS MCP dist | Fork |
| `docker/entrypoint.sh` | Start TS MCP process alongside FastAPI and deriver | Fork |

## Data Cleanup

One-time cleanup of orphaned observations written to the wrong collection (`slack-U04EXAMPLE/slack-U04EXAMPLE`) by the previous MCP implementation. Manual step, not part of the code fix.
