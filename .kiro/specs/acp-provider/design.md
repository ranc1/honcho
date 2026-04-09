# Design Document: ACP Provider for Honcho

## Overview

This feature adds an ACP (Agent Client Protocol) provider to Honcho that routes all LLM calls through an external ACP-compatible gateway via HTTP. Instead of calling LLM APIs directly, Honcho POSTs to the gateway's `/api/v1/acp/prompt` endpoint, which routes the request through ACP to an LLM-backed engine process.

The ACP provider intercepts at the provider dispatch level (`honcho_llm_call_inner`), so all existing reasoning modules (deriver, dreamer, dialectic, summarizer) work without modification. For agentic calls (dreamer, dialectic), Honcho's internal `_execute_tool_loop` is bypassed — the ACP provider constructs a combined prompt with tool instructions, and the gateway engine's native tool-calling loop handles MCP tool execution.

The fork also includes two MCP servers (an internal Python MCP for Honcho's reasoning modules and a client-facing TypeScript MCP for external consumers), fixes upstream bugs in the Conclusion API, and configures embeddings for a local open-source model.

## Components and Interfaces

### 1. Provider Type and Configuration

**Files:** `src/utils/types.py`, `src/config.py`

`"acp"` is added to `SupportedProviders`. Two new fields in `LLMSettings`:

| Setting | Env Var | Type | Default | Description |
|---|---|---|---|---|
| `ACP_GATEWAY_URL` | `LLM_ACP_GATEWAY_URL` | `str \| None` | `None` | Gateway bridge URL. Enables ACP provider when set. |
| `ACP_TIMEOUT_MS` | `LLM_ACP_TIMEOUT_MS` | `int` | `300000` | Request timeout (ms). Range: 1–600000. |

### 2. Client Registration and Dispatch

**File:** `src/utils/clients.py`

When `ACP_GATEWAY_URL` is set, the URL string is stored in `CLIENTS["acp"]`. Unlike other providers (which store SDK client objects), the ACP provider stores the raw URL. The dispatch detects this via `isinstance(client, str)`.

Two dispatch points intercept before the existing `match client:` blocks:
- `honcho_llm_call_inner()` — early return to `honcho_llm_call_inner_acp()`
- `handle_streaming_response()` — non-streaming call, yields single chunk with `is_done=True`

### 3. ACP Provider Module

**File:** `src/utils/acp_provider.py` (new, ~300 LOC)

#### Module Detection

`detect_module(messages, tools)` determines which Honcho module is calling:

```
tools present + delete_observations/finish_consolidation → "dreamer"
tools present + other                                    → "dialectic"
no tools + keyword match in prompt content               → deriver/dreamer/dialectic/summarizer
default fallback                                         → "summarizer"
```

Keywords per module:
- deriver: `["extract", "explicit", "atomic facts", "observation"]`
- dreamer: `["deductive", "inductive", "dream", "reasoning agent", "specialist"]`
- dialectic: `["query", "dialectic", "synthesis", "answer"]`
- summarizer: `["summarize", "summary", "recap", "concise"]`

#### Non-Agentic Path (Deriver, Summarizer)

1. Extract system/user messages from `messages` list
2. Concatenate user messages → `prompt`, system messages → `systemPrompt`
3. For deriver with `response_model`: append `honcho_extract_facts` tool instruction
4. POST `{ module, prompt, systemPrompt? }` to bridge
5. Bridge returns raw text (bridge is a dumb pipe — no extraction logic)
6. Deriver: fetch extraction result via `GET http://localhost:8000/mcp/extraction`. The CLI engine calls `honcho_extract_facts` MCP tool during the prompt, which stores structured JSON in the FastAPI process's extraction store. The deriver (separate process) reads it via this HTTP endpoint. Parse via `parse_deriver_response()` → `PromptRepresentation`. If no extraction result, fall back to parsing the raw text response.
7. Summarizer: return raw text

**Cross-process extraction flow:** The Honcho container runs two processes: the FastAPI API server (which hosts the MCP server and extraction store) and the deriver worker (`python -m src.deriver`). When the CLI engine calls `honcho_extract_facts`, the result is stored in the FastAPI process's memory. The deriver reads it via `GET /mcp/extraction` (HTTP, same container, different process). This is a single-slot store — consumed on read.

#### Agentic Path (Dreamer, Dialectic)

1. `build_agentic_prompt(messages, tools)` constructs combined prompt:
   - `[System Instructions]` — from system messages
   - `[Task]` — from user messages
   - `[Available Tools]` — each tool with internal name, description, parameters (no name mapping)
2. Tool names pass through as-is (internal names match MCP tool names)
3. Before the bridge call, if `tool_executor` is provided, register context with the MCP server via `POST /mcp/{module}/context` (sends serializable `ToolContext` fields extracted from `tool_executor._ctx`)
4. POST `{ module, prompt }` to bridge (no separate systemPrompt)
5. Engine handles tool calling via per-module MCP endpoints
6. After bridge returns (in `finally` block), deregister context via `DELETE /mcp/{module}/context`
7. Return raw text

#### Tool Name Mapping

`TOOL_NAME_MAP` has been removed. MCP tool names now match Honcho's internal tool names directly. The `build_agentic_prompt()` function passes tool names through as-is. See `.kiro/specs/mcp-internal-tools-migration/design.md` for the full migration design.

Two custom tools have no upstream equivalent:
- `honcho_extract_facts` — structured deriver output via extraction store (workaround for ACP's inability to enforce structured output schemas)
- `get_reasoning_chain` — reasoning chain traversal, dispatched via `tool_executor` closure which calls `_TOOL_HANDLERS["get_reasoning_chain"]` internally

#### Deriver Response Parsing

`parse_deriver_response(text, response_model)`:
- Empty → warning, return empty `PromptRepresentation`
- Valid JSON → `response_model.model_validate(data)`
- Invalid JSON → warning with preview, return empty `PromptRepresentation` (skip gracefully)

#### AcpLLMResponse

`@dataclass` compatible with `HonchoLLMCallResponse`:
- `content: Any` — parsed model for deriver, raw text for others
- Token counts default to 0 (not tracked by ACP)
- `finish_reasons` defaults to `["stop"]`

#### Bridge HTTP Contract

```
POST {ACP_GATEWAY_URL}/api/v1/acp/prompt

Request:  { "module": "deriver"|"dreamer"|"dialectic"|"summarizer",
            "prompt": "...",
            "systemPrompt": "..." }  // optional, non-agentic only

Response: { "text": "..." }
```

### 4. Conclusion Schema Fixes

**Files:** `src/schemas/api.py`, `src/crud/document.py`

Added `level: str | None` and `source_ids: list[str] | None` to both `Conclusion` and `ConclusionCreate` schemas.

Fixed `create_observations()` in `src/crud/document.py`:
- Changed `level="explicit"` → `getattr(obs, "level", None) or "explicit"`
- Added `source_ids=getattr(obs, "source_ids", None)` in 3 places (embedding path, non-embedding path, VectorRecord creation)

### 5. Embedding Configuration

**Files:** `src/embedding_client.py`, `src/models.py`, `migrations/versions/*.py`

- Embedding model: `openai/text-embedding-3-small` → `qwen3-embedding:0.6b` (open-source, 1024-dim, runs via Ollama)
- Vector dimensions: `Vector(1536)` → `Vector(1024)` in `MessageEmbedding` and `Document` models
- Migration scripts updated: `Vector(1536)` → `Vector(1024)` in `a1b2c3d4e5f6_initial_schema.py`, `917195d9b5e9_add_messageembedding_table.py`, and `119a52b73c60_support_external_embeddings.py`

### 6. MCP Servers

Two MCP servers run inside the Honcho Docker container:

#### Internal Tool MCP (Python, port 8000)

**File:** `src/mcp_server.py`

A Python MCP server implemented as a FastAPI router, mounted on the main Honcho FastAPI app. Runs in the same process as the Honcho API server. Exposes 16 tools matching Honcho's internal tool names, auto-generated from the `TOOLS` dict in `agent_tools.py`.

Per-module endpoints (`POST /mcp/{dreamer,dialectic,deriver}`) allow the MCP server to identify which module is calling. Before each ACP call, the calling process registers tool execution context via `POST /mcp/{module}/context` with serializable `ToolContext` fields. The MCP server creates a live `tool_executor` closure via `create_tool_executor()` and stores it. Tool dispatch calls the stored closure directly.

**Tools (16):**

15 internal tools dispatched via stored `tool_executor` closure + 1 custom tool:

| Tool | Used By | Description |
|---|---|---|
| `create_observations` | Dreamer | Create observations at any level (explicit, deductive, inductive, contradiction) |
| `delete_observations` | Dreamer | Batch delete observations by ID |
| `update_peer_card` | Dreamer | Set/update peer card biographical facts |
| `search_memory` | Dreamer, Dialectic | Semantic search across observations |
| `get_observation_context` | Dreamer, Dialectic | Retrieve messages by message IDs with surrounding context |
| `search_messages` | Dreamer, Dialectic | Semantic search across messages with conversation snippets |
| `grep_messages` | Dialectic | Exact text search across messages |
| `get_messages_by_date_range` | Dialectic | Temporal message retrieval |
| `search_messages_temporal` | Dialectic | Semantic search with date filtering |
| `get_recent_observations` | Dreamer | Most recent observations |
| `get_most_derived_observations` | Dreamer | Most frequently reinforced observations |
| `get_peer_card` | Dreamer, Dialectic | Read peer card biographical facts |
| `finish_consolidation` | Dreamer | Signal dreamer consolidation complete |
| `extract_preferences` | Dreamer | Extract preferences from conversation history |
| `get_reasoning_chain` | Dreamer, Dialectic | Traverse observation reasoning chains |
| `honcho_extract_facts` | Deriver | Structured fact extraction output (custom, extraction store) |

**Endpoints:**
- `POST /mcp/{module}` — JSON-RPC 2.0 MCP protocol (initialize, tools/list, tools/call)
- `POST /mcp/{module}/context` — Register tool execution context before ACP call
- `DELETE /mcp/{module}/context` — Deregister context after ACP call
- `GET /mcp/health` — Health check
- `GET /mcp/extraction` — Pop extraction result (single slot, consumed on read)

#### Client-Facing TS MCP (TypeScript/Node.js, port 8001)

**Directory:** `mcp/`

The upstream TypeScript MCP restored from the `main` branch, with only the entry point changed from Cloudflare Workers to a self-hosted Node.js HTTP server using `@modelcontextprotocol/sdk`'s `StreamableHTTPServerTransport`. All tool implementations (`mcp/src/tools/*.ts`) are reused as-is.

Serves external consumers (CLI engines during normal user conversations, integrations). Uses `@honcho-ai/sdk` to call Honcho's REST API — no shared memory or direct DB access.

**Tools (30):** All upstream tools including `query_conclusions`, `create_conclusions`, `chat`, `get_peer_card`, `get_session_messages`, `search`, `schedule_dream`, etc.

**Config:** `MCP_WORKSPACE_ID`, `HONCHO_BASE_URL`, `HONCHO_API_KEY` env vars.

#### Custom Tool: `honcho_extract_facts`

**Why it exists:** In standard Honcho, the deriver calls an LLM with `response_model=PromptRepresentation` to get structured JSON output. With the ACP provider, LLM calls are routed through the gateway's CLI engine, which cannot enforce structured output schemas. The deriver prompt instructs the CLI engine to call `honcho_extract_facts` with the extracted facts instead of outputting raw JSON. This tool writes the structured result to an in-memory extraction store. After the bridge returns, the deriver reads the result via `GET /mcp/extraction`.

**Fallback:** If the CLI engine doesn't call the tool, the deriver falls back to parsing the raw text response as JSON. If that also fails, extraction is skipped for that turn.

## Fork Surface

| File | Category |
|---|---|
| `src/utils/acp_provider.py` | ACP Provider (HTTP context registration, prompt construction) |
| `src/mcp_server.py` | Internal Tool MCP (16 tools, per-module endpoints, context registration) |
| `src/utils/clients.py` | ACP Provider (tool_executor threading) |
| `src/utils/agent_tools.py` | Core (1 line: attach `_ctx` to tool_executor closure) |
| `src/config.py` | ACP Provider (ACP_GATEWAY_URL, ACP_TIMEOUT_MS settings) |
| `src/utils/types.py` | ACP Provider ("acp" added to SupportedProviders) |
| `src/main.py` | MCP router mount |
| `src/schemas/api.py` | Schema Fix (level, source_ids on Conclusion) |
| `src/crud/document.py` | Schema Fix (level, source_ids in create_observations) |
| `src/embedding_client.py` | Embedding Config (qwen3-embedding:0.6b) |
| `src/models.py` | Embedding Config (Vector 1536→1024) |
| `migrations/versions/*.py` | Migration Fix (Vector 1536→1024) |
| `mcp/src/index.ts` | Client-Facing TS MCP (Node.js entry point) |
| `mcp/src/config.ts` | Client-Facing TS MCP (env var config) |
| `mcp/src/server.ts`, `mcp/src/tools/*.ts` | Client-Facing TS MCP (reused from upstream as-is) |
| `Dockerfile` | Node.js build stage for TS MCP |
| `docker/entrypoint.sh` | Start TS MCP + deriver + FastAPI |
| `tests/test_mcp_*.py` | MCP migration tests |

## ACP Gateway Requirements

The ACP gateway must:
1. Expose `POST /api/v1/acp/prompt` accepting `{ module, prompt, systemPrompt? }` and returning `{ text }` (raw text — bridge is a dumb pipe)
2. Register per-module MCP server URLs when creating ACP sessions for Honcho modules (e.g., `http://{honcho_host}:8000/mcp/dreamer` for the dreamer session, `http://{honcho_host}:8000/mcp/deriver` for the deriver session)
3. The internal MCP server provides 16 tools matching Honcho's internal tool names (see Section 6)

## Error Handling

| Condition | Behavior |
|---|---|
| HTTP non-200 from bridge | `RuntimeError` with status code and body |
| `httpx.TimeoutException` | `RuntimeError` with timeout duration |
| `httpx.ConnectError` | `RuntimeError` with connection details |
| Empty deriver response | Warning log, return empty `PromptRepresentation` |
| Invalid JSON deriver response | Warning log with preview, return empty `PromptRepresentation` |
| MCP tool HTTP error | `isError: true` with `"HTTP {status}: {body}"` |
| MCP tool exception | `isError: true` with error message |
