# Bugfix Requirements Document

## Introduction

The Python MCP server (`src/mcp_server.py`) was modeled after Honcho's upstream TypeScript MCP (designed for external API consumers) instead of Honcho's internal tool set (`src/utils/agent_tools.py`). This causes inefficient execution with data integrity issues when the ACP engine calls tools for the dreamer and dialectic modules:

- MCP tool names don't match the internal tool names the ACP prompt renders (e.g., `create_conclusions` vs `create_observations`)
- MCP parameter schemas don't match internal schemas (e.g., `conclusions: [string]` vs `observations: [{content, level, source_ids}]`)
- MCP tools require `peer_id`/`target_peer_id` params that internal tools don't use (ToolContext injects observer/observed), causing the engine to guess identity values — observations get written to the wrong collection (e.g., `slack-U04EXAMPLE/slack-U04EXAMPLE` instead of `agent-main-default/slack-U04EXAMPLE`)
- 5 message-access tools used by dreamer/dialectic have no MCP counterpart at all
- `TOOL_NAME_MAP` in `acp_provider.py` maps many-to-one (e.g., 5 distinct tools → `get_session_messages`) losing tool-specific behavior
- Semantic search returns empty results because the observer doesn't match the collection the data was written to

The net effect is wasted LLM cycles (the engine fails then retries with corrected params), observations written to wrong observer/observed collections, and blind semantic searches that return empty results. Some tools do eventually succeed after retries, but the data ends up in the wrong place.

## Implementation Constraint

All changes MUST be confined to fork-only files (`src/mcp_server.py`, `src/utils/acp_provider.py`) except for two minimal core code changes: threading `observer`/`observed` params through `honcho_llm_call()` and `honcho_llm_call_inner()` in `clients.py`, and passing them from the call site in `specialists.py`. This is justified because the ACP provider replaces in-process tool execution with out-of-process MCP tool execution — identity that previously flowed through the `tool_executor` closure must now flow through the provider call chain. No other core Honcho code (agent_tools.py, prompts, routers, schemas, crud, models, config, etc.) may be modified.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the ACP engine receives a dreamer prompt with tool `create_observations` (internal name) THEN the system maps it to `create_conclusions` via TOOL_NAME_MAP, but the MCP server's `create_conclusions` expects `{peer_id, target_peer_id, conclusions: [string]}` while the prompt describes `{observations: [{content, level, source_ids}]}`, causing parameter validation failure

1.2 WHEN the ACP engine receives a dreamer prompt with tool `delete_observations` (internal name) THEN the system maps it to `delete_conclusion` (singular) via TOOL_NAME_MAP, but the MCP server expects `{peer_id, target_peer_id, conclusion_id: string}` while the prompt describes `{observation_ids: [string]}`, causing parameter validation failure

1.3 WHEN the ACP engine receives a dreamer prompt with tool `update_peer_card` (internal name) THEN the system maps it to `set_peer_card` via TOOL_NAME_MAP, but the MCP server expects `{peer_id, peer_card: [string]}` while the prompt describes `{content: [string]}`, causing parameter validation failure

1.4 WHEN the ACP engine receives a dreamer prompt with tool `search_memory` (internal name) THEN the system maps it to `query_conclusions` via TOOL_NAME_MAP, but the MCP server's `query_conclusions` expects `{peer_id, query, target_peer_id?, top_k?}` while the prompt describes `{query, top_k?}` (no peer_id), causing parameter validation failure

1.5 WHEN the ACP engine receives a prompt with any of `search_messages`, `grep_messages`, `get_messages_by_date_range`, `search_messages_temporal`, or `extract_preferences` THEN the system maps all five to `get_session_messages` via TOOL_NAME_MAP, but no MCP tool named `get_session_messages` exists, causing tool-not-found failure

1.6 WHEN the ACP engine receives a dreamer prompt with tool `get_recent_observations` or `get_most_derived_observations` THEN the system maps both to `list_conclusions` via TOOL_NAME_MAP, losing the distinct behavior of each tool (recent vs most-derived), and the MCP server's `list_conclusions` expects `{peer_id}` while the prompt describes `{limit?, session_only?}` or `{limit?}`, causing parameter mismatch

1.7 WHEN the ACP engine receives a dreamer prompt with tool `get_observation_context` THEN the system maps it to `query_conclusions` via TOOL_NAME_MAP, but `query_conclusions` performs semantic search while `get_observation_context` retrieves messages by message IDs — completely different operations

1.8 WHEN the ACP engine receives a dreamer prompt with tool `finish_consolidation` THEN the system maps it to `query_conclusions` via TOOL_NAME_MAP, but `query_conclusions` performs semantic search while `finish_consolidation` signals completion — completely different operations

1.9 WHEN the ACP engine receives a prompt with tool `get_reasoning_chain` (internal name) THEN the system maps it to `honcho_get_reasoning_chain` via TOOL_NAME_MAP, and while the MCP handler reuses the internal handler, the parameter name differs: MCP expects `conclusion_id` while the internal schema uses `observation_id`

1.10 WHEN any MCP tool other than `honcho_get_reasoning_chain` and `honcho_extract_facts` is dispatched THEN the system makes HTTP calls to Honcho's REST API instead of reusing internal `_TOOL_HANDLERS` directly, adding unnecessary network overhead and requiring peer_id/target_peer_id parameters that the internal handlers don't need

### Expected Behavior (Correct)

2.1 WHEN the ACP engine receives a dreamer prompt with tool `create_observations` THEN the system SHALL expose an MCP tool named `create_observations` with the same parameter schema as the internal tool (`{observations: [{content, level, source_ids, ...}]}`) and dispatch to `_TOOL_HANDLERS["create_observations"]` via a shared ToolContext

2.2 WHEN the ACP engine receives a dreamer prompt with tool `delete_observations` THEN the system SHALL expose an MCP tool named `delete_observations` with the same parameter schema as the internal tool (`{observation_ids: [string]}`) and dispatch to `_TOOL_HANDLERS["delete_observations"]` via a shared ToolContext

2.3 WHEN the ACP engine receives a dreamer prompt with tool `update_peer_card` THEN the system SHALL expose an MCP tool named `update_peer_card` with the same parameter schema as the internal tool (`{content: [string]}`) and dispatch to `_TOOL_HANDLERS["update_peer_card"]` via a shared ToolContext

2.4 WHEN the ACP engine receives a prompt with tool `search_memory` THEN the system SHALL expose an MCP tool named `search_memory` with the same parameter schema as the internal tool (`{query, top_k?}`) and dispatch to `_TOOL_HANDLERS["search_memory"]` via a shared ToolContext

2.5 WHEN the ACP engine receives a prompt with any of `search_messages`, `grep_messages`, `get_messages_by_date_range`, `search_messages_temporal`, or `extract_preferences` THEN the system SHALL expose individual MCP tools for each with matching parameter schemas and dispatch each to its corresponding `_TOOL_HANDLERS` entry via a shared ToolContext

2.6 WHEN the ACP engine receives a dreamer prompt with tool `get_recent_observations` or `get_most_derived_observations` THEN the system SHALL expose individual MCP tools for each with matching parameter schemas and dispatch each to its corresponding `_TOOL_HANDLERS` entry via a shared ToolContext

2.7 WHEN the ACP engine receives a dreamer prompt with tool `get_observation_context` THEN the system SHALL expose an MCP tool named `get_observation_context` with the same parameter schema (`{message_ids: [string]}`) and dispatch to `_TOOL_HANDLERS["get_observation_context"]` via a shared ToolContext

2.8 WHEN the ACP engine receives a dreamer prompt with tool `finish_consolidation` THEN the system SHALL expose an MCP tool named `finish_consolidation` with the same parameter schema (`{summary: string}`) and dispatch to `_TOOL_HANDLERS["finish_consolidation"]` via a shared ToolContext

2.9 WHEN the ACP engine receives a prompt with tool `get_reasoning_chain` THEN the system SHALL expose an MCP tool named `get_reasoning_chain` (dropping the `honcho_` prefix) with the same parameter schema as the internal tool (`{observation_id, direction?}`) and dispatch to `_TOOL_HANDLERS["get_reasoning_chain"]` via a shared ToolContext

2.10 WHEN any of the 15 internal tools is called via MCP THEN the system SHALL dispatch directly to the corresponding `_TOOL_HANDLERS` function using a shared ToolContext (with `observer` and `observed` threaded through the provider call chain from the specialist and stored in a module-level identity dict by the ACP provider before the gateway call), without making HTTP calls to the REST API. The ToolContext SHALL be constructed at dispatch time with `workspace_name` from `MCP_WORKSPACE_ID`, `session_name=None`, `current_messages=None`, `include_observation_ids=True`, and a module-level `asyncio.Lock` for `db_lock`. This uses the same async-flow pattern as the extraction store (`_extraction_result`): set before `await`, read during tool dispatch, same process.

2.11 WHEN `build_agentic_prompt()` in `acp_provider.py` renders tool schemas for the ACP prompt THEN the system SHALL remove `TOOL_NAME_MAP` and pass internal tool names and schemas through as-is without any name mapping, since MCP tool names now match internal names. The `[Available Tools]` section SHALL continue to be rendered using the internal tool names and schemas directly. NOTE: This creates a coupling between the `TOOLS` dict in `agent_tools.py` (core code, read-only) and `MCP_TOOL_DEFS` in `mcp_server.py` (fork code). The prompt renders schemas from `TOOLS`; the engine calls MCP tools defined by `MCP_TOOL_DEFS`. Both must have matching tool names and parameter schemas. If upstream Honcho changes a tool schema in `agent_tools.py`, `mcp_server.py` must be updated to match. This is an accepted maintenance cost of the no-core-changes constraint.

2.12 WHEN the MCP server is initialized THEN the system SHALL expose exactly 16 tools:

Internal tools dispatched via `_TOOL_HANDLERS` (15):
1. `create_observations` — create observations at any level (explicit, deductive, inductive, contradiction)
2. `delete_observations` — batch delete observations by ID
3. `update_peer_card` — set/update peer card facts
4. `search_memory` — semantic search across observations
5. `get_observation_context` — retrieve messages by message IDs with surrounding context
6. `search_messages` — semantic search across messages with conversation snippets
7. `grep_messages` — exact text search across messages
8. `get_messages_by_date_range` — temporal message retrieval
9. `search_messages_temporal` — semantic search with date filtering
10. `get_recent_observations` — most recent observations
11. `get_most_derived_observations` — most frequently reinforced observations
12. `get_peer_card` — read peer card biographical facts
13. `finish_consolidation` — signal dreamer consolidation complete
14. `extract_preferences` — extract preferences from conversation history
15. `get_reasoning_chain` — traverse observation reasoning chains

Custom tool (1):
16. `honcho_extract_facts` — structured deriver output via extraction store

Removed: `chat` (REST API endpoint, not in `_TOOL_HANDLERS`, not in any specialist tool list — engine discovered it from `tools/list` and called it with guessed identity values).

Excluded from `_TOOL_HANDLERS`: `get_recent_history` and `get_session_summary` (not in any module's tool list).

2.13 WHEN the fix is deployed THEN the system SHOULD clean up orphaned observations written to the wrong collection (`slack-U04EXAMPLE/slack-U04EXAMPLE`) by the previous MCP implementation. These observations and peer card entries should be deleted or migrated to the correct collection (`agent-main-default/slack-U04EXAMPLE`). This is a one-time manual cleanup step.

2.14 WHEN the internal tool MCP (`honcho-tool-mcp`) replaces the current Python MCP at `/mcp` THEN the system SHALL also restore a separate client-facing MCP (`honcho-mcp`) that exposes the full upstream TypeScript MCP tool set (24 tools) for external consumers (CLI engine during normal user conversations, future integrations). The client-facing MCP SHALL reuse the original upstream TypeScript MCP code from the `main` branch (`mcp/src/server.ts`, `mcp/src/tools/*.ts`) with only the entry point changed from Cloudflare Workers to a self-hosted Node.js HTTP server. The tool implementations, SDK usage, and server setup MUST NOT be rewritten — only the hosting layer changes. The client-facing MCP SHALL run as a separate process in the same Docker container on a different port (e.g., 8001), alongside the FastAPI server (port 8000). Node.js SHALL be added back to the Docker image to support this. Config (workspace ID, Honcho base URL) SHALL be passed via environment variables or MCP request headers.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the `honcho_extract_facts` MCP tool is called by the ACP engine during deriver prompts THEN the system SHALL CONTINUE TO store the extraction result in the module-level extraction store and serve it via `GET /mcp/extraction`

3.2 WHEN the MCP JSON-RPC endpoint receives `initialize`, `tools/list`, or `tools/call` requests THEN the system SHALL CONTINUE TO respond with valid JSON-RPC 2.0 responses using protocol version `2024-11-05`

3.3 WHEN the `GET /mcp/health` endpoint is called THEN the system SHALL CONTINUE TO return `{"status": "ok"}`

3.4 WHEN the `GET /mcp/extraction` endpoint is called THEN the system SHALL CONTINUE TO pop and return the extraction result (single-slot, consumed on read)

3.5 WHEN non-ACP providers are used (Anthropic, OpenAI, etc.) THEN the system SHALL CONTINUE TO use `_execute_tool_loop` with internal `_TOOL_HANDLERS` directly, unaffected by MCP server changes

3.6 WHEN the deriver module makes a non-agentic ACP call THEN the system SHALL CONTINUE TO append the `honcho_extract_facts` instruction and fetch the extraction result via `GET /mcp/extraction`

3.7 WHEN `detect_module()` in `acp_provider.py` classifies a call as dreamer, dialectic, deriver, or summarizer THEN the system SHALL CONTINUE TO use the same detection logic (tools present + tool name heuristics for agentic, keyword matching for non-agentic). NOTE: `detect_module()` checks for `delete_observations` and `finish_consolidation` in tool names to identify dreamer calls. Since MCP tool names now match internal names, this continues to work. This implicit dependency on tool names should be documented — renaming these tools would silently break module detection.

3.8 WHEN the ACP provider constructs non-agentic prompts (deriver, summarizer) THEN the system SHALL CONTINUE TO extract system/user messages and POST to the ACP gateway with `{module, prompt, systemPrompt?}`
