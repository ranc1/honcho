# Design Document: ACP Provider for Honcho

## Overview

This feature adds an ACP (Agent Client Protocol) provider to Honcho that routes all LLM calls through an external ACP-compatible gateway via HTTP. Instead of calling LLM APIs directly, Honcho POSTs to the gateway's `/api/v1/acp/prompt` endpoint, which routes the request through ACP to an LLM-backed engine process.

The ACP provider intercepts at the provider dispatch level (`honcho_llm_call_inner`), so all existing reasoning modules (deriver, dreamer, dialectic, summarizer) work without modification. For agentic calls (dreamer, dialectic), Honcho's internal `_execute_tool_loop` is bypassed — the ACP provider constructs a combined prompt with tool instructions, and the gateway engine's native tool-calling loop handles MCP tool execution.

The fork also fixes upstream bugs in the Conclusion API and configures embeddings for a local open-source model.

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
5. Deriver: parse JSON response via `parse_deriver_response()` → `PromptRepresentation`
6. Summarizer: return raw text

#### Agentic Path (Dreamer, Dialectic)

1. `build_agentic_prompt(messages, tools)` constructs combined prompt:
   - `[System Instructions]` — from system messages
   - `[Task]` — from user messages
   - `[Available Tools]` — each tool with mapped MCP name, description, parameters
2. Tool names mapped via `TOOL_NAME_MAP`
3. POST `{ module, prompt }` to bridge (no separate systemPrompt)
4. Return raw text (engine handles tool calling via MCP)

#### Tool Name Mapping

`TOOL_NAME_MAP` maps Honcho internal names → canonical MCP names (matching `honcho/mcp/src/tools/`):

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
- `honcho_get_reasoning_chain` — reasoning chain traversal via `source_ids`
- `honcho_extract_facts` — structured deriver output (referenced in prompt instruction only)

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

**Files:** `src/embedding_client.py`, `src/models.py`

- Embedding model: `openai/text-embedding-3-small` → `qwen3-embedding:0.6b` (open-source, 1024-dim, runs via Ollama)
- Vector dimensions: `Vector(1536)` → `Vector(1024)` in `MessageEmbedding` and `Document` models

## Fork Surface

~350 LOC across 8 files:

| File | LOC Changed | Category |
|---|---|---|
| `src/utils/acp_provider.py` | ~300 (new) | ACP Provider |
| `src/utils/clients.py` | ~35 | ACP Provider |
| `src/config.py` | ~3 | ACP Provider |
| `src/utils/types.py` | ~2 | ACP Provider |
| `src/schemas/api.py` | ~4 | Schema Fix |
| `src/crud/document.py` | ~7 | Schema Fix |
| `src/embedding_client.py` | ~2 | Embedding Config |
| `src/models.py` | ~4 | Embedding Config |

## ACP Gateway Requirements

The ACP gateway must:
1. Expose `POST /api/v1/acp/prompt` accepting `{ module, prompt, systemPrompt? }` and returning `{ text }`
2. Provide MCP tools matching Honcho's canonical names (`query_conclusions`, `create_conclusions`, `delete_conclusion`, `set_peer_card`, `get_peer_card`, `list_conclusions`, `get_session_messages`, `chat`)
3. Provide a `honcho_extract_facts` MCP tool for structured deriver output
4. Optionally provide `honcho_get_reasoning_chain` for reasoning chain traversal

## Error Handling

| Condition | Behavior |
|---|---|
| HTTP non-200 from bridge | `RuntimeError` with status code and body |
| `httpx.TimeoutException` | `RuntimeError` with timeout duration |
| `httpx.ConnectError` | `RuntimeError` with connection details |
| Empty deriver response | Warning log, return empty `PromptRepresentation` |
| Invalid JSON deriver response | Warning log with preview, return empty `PromptRepresentation` |
