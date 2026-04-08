# Design Document: ACP Provider for Honcho

## Overview

This feature adds an ACP (Agent Client Protocol) provider to Honcho that routes all LLM calls through an external ACP-compatible gateway via HTTP. Instead of calling LLM APIs directly, Honcho POSTs to the gateway's `/api/v1/acp/prompt` endpoint, which routes the request through ACP to an LLM-backed engine process.

The ACP provider intercepts at the provider dispatch level (`honcho_llm_call_inner`), so all existing reasoning modules (deriver, dreamer, dialectic, summarizer) work without modification. For agentic calls (dreamer, dialectic), Honcho's internal `_execute_tool_loop` is bypassed — the ACP provider constructs a combined prompt with tool instructions, and the gateway engine's native tool-calling loop handles MCP tool execution.

The fork also includes a Python MCP server (running inside the FastAPI process), fixes upstream bugs in the Conclusion API, and configures embeddings for a local open-source model.

## Components and Interfaces

### 1. Provider Type and Configuration

**Files:** `src/utils/types.py`, `src/config.py`

`"acp"` is added to `SupportedProviders`. Two new fields in `LLMSettings`:

| Setting | Env Var | Type | Default | Description |
|---|---|---|---|---|
| `ACP_GATEWAY_URL` | `LLM_ACP_GATEWAY_URL` | `str \| None` | `None` | Gateway bridge URL. Enables ACP provider when set. |
| `ACP_TIMEOUT_MS` | `LLM_ACP_TIMEOUT_MS` | `int` | `120000` | Request timeout (ms). Range: 1–600000. |

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
   - `[Available Tools]` — each tool with mapped MCP name, description, parameters
2. Tool names mapped via `TOOL_NAME_MAP`
3. POST `{ module, prompt }` to bridge (no separate systemPrompt)
4. Return raw text (engine handles tool calling via MCP)

#### Tool Name Mapping

`TOOL_NAME_MAP` maps Honcho internal names → canonical MCP names:

| Honcho Internal | Canonical MCP Name |
|---|---|
| `search_memory` | `query_conclusions` |
| `create_observations` | `create_conclusions` |
| `delete_observations` | `delete_conclusion` |
| `update_peer_card` | `set_peer_card` |
| `get_reasoning_chain` | `honcho_get_reasoning_chain` (custom) |
| `search_messages` / `grep_messages` | `get_session_messages` |
| `get_recent_observations` / `get_most_derived_observations` | `list_conclusions` |
| `get_observation_context` / `finish_consolidation` | `query_conclusions` |
| `get_peer_card` | `get_peer_card` |
| `extract_preferences` / `get_messages_by_date_range` / `search_messages_temporal` | `get_session_messages` |

Two custom names have no upstream equivalent:
- `honcho_get_reasoning_chain` — reasoning chain traversal, reuses Honcho's internal `_handle_get_reasoning_chain` handler directly (same process, direct DB access via `tracked_db()`)
- `honcho_extract_facts` — structured deriver output via extraction store (workaround for ACP's inability to enforce structured output schemas)

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

### 6. Python MCP Server

**File:** `src/mcp_server.py` (new, ~280 LOC)

A Python MCP server implemented as a FastAPI router, mounted on the main Honcho FastAPI app at `/mcp`. Runs in the same process as the Honcho API server. Exposes 10 tools matching the canonical MCP tool names.

**Why Python, not TypeScript:** The original upstream MCP was a Cloudflare Workers TypeScript app. We migrated it to Python running inside the FastAPI process. Benefits:
1. No Node.js in the Docker image — pure Python container
2. `honcho_get_reasoning_chain` reuses Honcho's internal `_handle_get_reasoning_chain` handler directly (same process, direct DB access)
3. `honcho_extract_facts` stores results in a module-level variable accessible via `GET /mcp/extraction` for the deriver process

**Tools (10):**

8 standard tools expose Honcho's REST API to the CLI engine. 2 custom tools fill gaps created by the ACP architecture.

| Tool | Implementation | Used By | Description |
|---|---|---|---|
| `list_conclusions` | `POST /conclusions/list` with `observer_id`/`observed_id` filters | Dreamer | List observations for consolidation |
| `query_conclusions` | `POST /conclusions/query` with filters | Dreamer, Dialectic | Semantic search over observations |
| `create_conclusions` | `POST /conclusions` with `observer_id`/`observed_id` in body | Dreamer | Create deductive/inductive observations during consolidation |
| `delete_conclusion` | `DELETE /conclusions/{id}` | Dreamer | Remove redundant/outdated observations during consolidation |
| `chat` | `POST /peers/{pid}/chat` | Dialectic | Trigger dialectic reasoning query |
| `get_peer_card` | `GET /peers/{pid}/card?target=...` | Dreamer, Dialectic | Read biographical facts |
| `set_peer_card` | `PUT /peers/{pid}/card?target=...` | Dreamer | Update biographical facts during consolidation |
| `get_peer_context` | `GET /peers/{pid}/context?target=...&search_query=...` | Dialectic | Combined representation + peer card retrieval |
| `honcho_get_reasoning_chain` | Internal `_handle_get_reasoning_chain` (direct DB) | Dreamer | Traverse observation source chains (custom) |
| `honcho_extract_facts` | Module-level extraction store | Deriver | Structured fact extraction output (custom) |

#### Custom Tool: `honcho_extract_facts`

**Why it exists:** In standard Honcho, the deriver calls an LLM with `response_model=PromptRepresentation` to get structured JSON output (a list of explicit observations). With the ACP provider, LLM calls are routed through the gateway's CLI engine, which cannot enforce structured output schemas. The deriver prompt instructs the CLI engine to call `honcho_extract_facts` with the extracted facts instead of outputting raw JSON. This tool writes the structured result to an in-memory extraction store. After the bridge returns, the deriver reads the result via `GET /mcp/extraction`.

**Used by:** Deriver module (non-agentic path). The ACP provider appends this instruction to every deriver prompt: "You MUST submit your extracted facts by calling the honcho_extract_facts tool."

**Fallback:** If the CLI engine doesn't call the tool (unreliable tool-calling), the deriver falls back to parsing the raw text response as JSON. If that also fails, extraction is skipped for that turn — messages remain in Honcho and will be reprocessed on the next deriver run.

#### Custom Tool: `honcho_get_reasoning_chain`

**Why it exists:** In standard Honcho, the dreamer agent calls `get_reasoning_chain` as an internal tool during its `_execute_tool_loop`. With the ACP provider, the dreamer's tool loop is bypassed — the ACP provider constructs a combined prompt with `[Available Tools]` and the CLI engine handles tool calling via MCP. The CLI engine needs an MCP-accessible version of this tool.

**Used by:** Dreamer module (agentic path). During consolidation, the dreamer traverses observation source chains to understand how deductive/inductive observations were derived before deciding whether to consolidate or delete them.

**Implementation:** Reuses Honcho's internal `_handle_get_reasoning_chain` handler directly (same FastAPI process, direct DB access via `tracked_db()`). Supports `direction` parameter (`premises`, `conclusions`, `both`). Returns formatted markdown with observation content, premises/sources, and derived conclusions.

**Workspace ID:** Configured via `MCP_WORKSPACE_ID` env var (set in docker-compose), falling back to `settings.NAMESPACE`.

**Endpoints:**
- `POST /mcp` — JSON-RPC 2.0 MCP protocol (initialize, tools/list, tools/call)
- `GET /mcp/health` — Health check
- `GET /mcp/extraction` — Pop extraction result (single slot, consumed on read, used by deriver process)

**Extraction store:** Module-level `_extraction_result: str | None`. `store_extraction_result(json)` writes, `pop_extraction_result()` reads and clears. The deriver process (separate Python process in the same container) reads via `GET /mcp/extraction`.

**JSON-RPC protocol:** Uses protocol version `2024-11-05`, matching kiro-cli's expected format. Simple JSON-RPC request/response — no Streamable HTTP transport (kiro-cli doesn't support it).

## Fork Surface

~400 LOC across 8+ files:

| File | LOC Changed | Category |
|---|---|---|
| `src/utils/acp_provider.py` | ~300 (new) | ACP Provider |
| `src/mcp_server.py` | ~280 (new) | Python MCP Server |
| `src/utils/clients.py` | ~35 | ACP Provider |
| `src/config.py` | ~3 | ACP Provider |
| `src/utils/types.py` | ~2 | ACP Provider |
| `src/main.py` | ~2 | MCP router mount |
| `src/schemas/api.py` | ~4 | Schema Fix |
| `src/crud/document.py` | ~7 | Schema Fix |
| `src/embedding_client.py` | ~2 | Embedding Config |
| `src/models.py` | ~4 | Embedding Config |
| `migrations/versions/*.py` | ~4 | Migration Fix (Vector 1536→1024) |
| `Dockerfile` | ~0 | Pure Python (no Node.js) |

The `mcp/` directory contains the original upstream TypeScript MCP server files (retained for reference) but they are not used at runtime. The Python MCP server in `src/mcp_server.py` replaces them entirely.

## ACP Gateway Requirements

The ACP gateway must:
1. Expose `POST /api/v1/acp/prompt` accepting `{ module, prompt, systemPrompt? }` and returning `{ text }` (raw text — bridge is a dumb pipe)
2. Register the Honcho MCP server (running inside the Honcho FastAPI process at `http://{honcho_host}:8000/mcp`) as an MCP server with the CLI engine at session creation
3. The Honcho MCP server provides 10 tools: `list_conclusions`, `query_conclusions`, `create_conclusions`, `delete_conclusion`, `chat`, `get_peer_card`, `set_peer_card`, `get_peer_context`, `honcho_get_reasoning_chain`, `honcho_extract_facts`

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
