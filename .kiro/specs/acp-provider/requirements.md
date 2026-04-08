# Requirements Document

## Introduction

Honcho's reasoning modules (deriver, dreamer, dialectic, summarizer) require an LLM provider to function. The upstream codebase supports Anthropic, OpenAI, Google, Groq, custom, and vLLM providers — all of which call LLM APIs directly.

This feature adds an ACP (Agent Client Protocol) provider that routes all LLM calls through an external ACP-compatible gateway via HTTP instead of calling LLM APIs directly. This enables any ACP gateway to serve as the LLM backend for Honcho's reasoning modules, preserving all native Honcho intelligence (fact extraction, dream cycles, dialectic agents, session summarization) while delegating LLM interactions to the gateway's engine process.

The fork also fixes upstream bugs in the Conclusion API where observation levels were hardcoded and schema fields were missing, and configures the embedding subsystem for a local open-source model (`qwen3-embedding:0.6b`).

## Requirements

### Requirement 1: Schema Fixes for Observation Levels

**User Story:** As a developer, I want Honcho to correctly store and return observation levels and reasoning chains, so that the full reasoning hierarchy (explicit, deductive, inductive, contradiction) is preserved and queryable.

#### Acceptance Criteria

1. WHEN a `create_observations` request includes an observation with a `level` field, THE system SHALL use the provided `level` value instead of hardcoding `"explicit"`.
2. THE system SHALL include `level` and `source_ids` fields in the `Conclusion` response schema (`src/schemas/api.py`).
3. THE system SHALL include `level` and `source_ids` fields in the `ConclusionCreate` input schema (`src/schemas/api.py`).
4. WHEN a `Conclusion` is retrieved via the REST API, THE system SHALL return the stored `level` and `source_ids` values in the response body.
5. THE `create_observations` CRUD function SHALL pass through `source_ids` from the API request to the database model, in both the embedding and non-embedding code paths.
6. THE `create_documents` function SHALL pass through `source_ids` when creating `VectorRecord` entries.

### Requirement 2: ACP Provider Module

**User Story:** As a developer, I want Honcho to route LLM calls through an external ACP gateway instead of calling an LLM API directly, so that any ACP-compatible engine can serve as the LLM backend for Honcho's reasoning modules.

#### Acceptance Criteria

1. THE system SHALL add `"acp"` to the `SupportedProviders` literal type (`src/utils/types.py`).
2. THE system SHALL add `LLM_ACP_GATEWAY_URL` (string, default: None) and `LLM_ACP_TIMEOUT_MS` (integer, default: 120000, range: 1–600000) to `LLMSettings` (`src/config.py`).
3. WHEN `LLM_ACP_GATEWAY_URL` is set, THE system SHALL register the URL string in the `CLIENTS` dict under the `"acp"` key (`src/utils/clients.py`).
4. THE `honcho_llm_call_inner()` function SHALL dispatch to the ACP provider when `provider == "acp"` and `isinstance(client, str)`, before the existing `match client:` block.
5. THE `handle_streaming_response()` function SHALL dispatch to the ACP provider for the streaming path, making a non-streaming call and yielding a single chunk with `is_done=True`.
6. THE ACP provider module (`src/utils/acp_provider.py`) SHALL implement `honcho_llm_call_inner_acp()` matching the same interface as other provider implementations.
7. WHEN handling a non-agentic call (deriver or summarizer), THE ACP provider SHALL extract system and user messages, POST `{ module, prompt, systemPrompt? }` to `{gateway_url}/api/v1/acp/prompt`, and return the text response.
8. WHEN handling a deriver call with `response_model`, THE ACP provider SHALL append an instruction to the prompt telling the engine to call the `honcho_extract_facts` MCP tool. After receiving the bridge response, it SHALL first check the co-located MCP extraction store (`GET http://localhost:{MCP_PORT}/extraction`) for structured output from the tool. If found, parse it via `parse_deriver_response()`. If not found, fall back to parsing the raw text response. IF neither produces valid JSON, log a warning and return an empty result (extraction skipped).
9. WHEN handling an agentic call (dreamer or dialectic), THE ACP provider SHALL construct a combined prompt with `[System Instructions]`, `[Task]`, and `[Available Tools]` sections. Tool names SHALL be mapped from Honcho internal names to canonical MCP names via `TOOL_NAME_MAP`. The gateway engine's tool-calling loop handles MCP tool execution; Honcho's `_execute_tool_loop` is bypassed.
10. THE ACP provider SHALL detect the calling module via `detect_module()`: tools present with `delete_observations`/`finish_consolidation` → dreamer; tools present otherwise → dialectic; no tools + keyword match → deriver/dreamer/dialectic/summarizer; default → summarizer.
11. THE `TOOL_NAME_MAP` SHALL map Honcho internal tool names to canonical MCP names matching the official Honcho MCP server: `search_memory` → `query_conclusions`, `create_observations` → `create_conclusions`, `delete_observations` → `delete_conclusion`, `update_peer_card` → `set_peer_card`, `get_reasoning_chain` → `honcho_get_reasoning_chain`, `search_messages`/`grep_messages` → `get_session_messages`, `get_recent_observations`/`get_most_derived_observations` → `list_conclusions`, `get_peer_card` → `get_peer_card`, `finish_consolidation` → `query_conclusions`, `extract_preferences` → `get_session_messages`.
12. THE `AcpLLMResponse` dataclass SHALL be compatible with `HonchoLLMCallResponse` fields (content, token counts, finish_reasons, tool_calls_made, thinking fields).
13. THE ACP provider SHALL handle HTTP errors (non-200), timeouts (`httpx.TimeoutException`), and connection failures (`httpx.ConnectError`) by raising `RuntimeError` with descriptive messages.

### Requirement 3: Embedding Configuration

**User Story:** As a developer, I want Honcho to use a local open-source embedding model for vector operations, so that the system can run without external API keys for embeddings.

#### Acceptance Criteria

1. THE embedding client SHALL use `qwen3-embedding:0.6b` as the default model for the `openrouter` provider (`src/embedding_client.py`).
2. THE SQLAlchemy vector column definitions SHALL use `Vector(1024)` dimensions for both `MessageEmbedding.embedding` and `Document.embedding` (`src/models.py`), matching the output dimensions of `qwen3-embedding:0.6b`.
3. THE Alembic migration scripts SHALL use `Vector(1024)` dimensions in all `embedding` column definitions, matching the model and SQLAlchemy definitions.

### Requirement 4: Standalone MCP Server

**User Story:** As a developer, I want the Honcho fork to include a standalone MCP server that runs alongside the API in Docker, so that the CLI engine can access Honcho tools and the deriver can use structured extraction.

#### Acceptance Criteria

1. THE MCP server (`src/mcp_server.py`) SHALL run as a FastAPI router mounted on the main Honcho app at `POST /mcp`.
2. THE MCP server SHALL expose all upstream Honcho canonical tools (`list_conclusions`, `query_conclusions`, `create_conclusions`, `delete_conclusion`, `chat`, `get_peer_card`, `set_peer_card`, `get_peer_context`) using the original upstream tool files.
3. THE MCP server SHALL expose `honcho_extract_facts` (custom) which writes structured deriver output to a single-slot in-memory extraction store.
4. THE MCP server SHALL expose `honcho_get_reasoning_chain` (custom) which traverses `source_ids` via BFS using Honcho's REST API.
5. THE MCP server SHALL expose `GET /extraction` to allow the ACP provider to read and consume the extraction result.
6. THE MCP server SHALL be configured via environment variables: `MCP_PORT` (default 3100), `HONCHO_BASE_URL`, `HONCHO_API_KEY`, `HONCHO_WORKSPACE_ID`.
7. THE Dockerfile SHALL build the MCP server in a separate stage (Node.js) and copy the output to the final image alongside the Python app.
8. THE Docker container command SHALL start the MCP server alongside the API and deriver.
