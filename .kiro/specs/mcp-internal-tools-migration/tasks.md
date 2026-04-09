# Implementation Plan

- [x] 1. Revert previous implementation residue
  - Revert `src/utils/clients.py` to remove `observer`/`observed` params from all `honcho_llm_call` and `honcho_llm_call_inner` overloads (added in previous attempt). Keep the `tool_executor` threading if already present, otherwise it will be added in task 3.3.
  - Revert `src/dreamer/specialists.py` to remove `observer=observer, observed=observed` from `honcho_llm_call()` call
  - Revert `src/dialectic/core.py` to remove `observer=self.observer, observed=self.observed` from both `honcho_llm_call()` calls
  - Verify `src/mcp_server.py` and `src/utils/acp_provider.py` are clean (will be fully rewritten in later tasks)
  - _Requirements: Implementation Constraint — no changes to specialists.py or dialectic/core.py_

- [x] 2. Core code change: attach `_ctx` to tool_executor closure in `src/utils/agent_tools.py`
  - In `create_tool_executor()`, add one line before `return execute_tool`:
    ```python
    execute_tool._ctx = ctx  # type: ignore[attr-defined]
    ```
  - This lets the ACP provider read `tool_executor._ctx.observer`, `tool_executor._ctx.observed`, etc.
  - No function signature changes. No other modifications to agent_tools.py.
  - _Requirements: 2.10_

- [x] 3. Core code change: thread `tool_executor` through `honcho_llm_call_inner()` in `src/utils/clients.py`
  - Add `tool_executor: Callable | None = None` to all `honcho_llm_call_inner()` overloads + implementation
  - Add `tool_executor: Callable | None = None` to `handle_streaming_response()`
  - In `honcho_llm_call`'s `_call_with_provider_selection`, pass `tool_executor=tool_executor` to `honcho_llm_call_inner()`
  - In the ACP branch of `honcho_llm_call_inner()`, pass `tool_executor=tool_executor` to `honcho_llm_call_inner_acp()`
  - In `handle_streaming_response`'s ACP branch, pass `tool_executor=tool_executor` to `honcho_llm_call_inner_acp()`
  - No new params on `honcho_llm_call()` — `tool_executor` already exists there
  - _Requirements: 2.10_

- [x] 4. Rewrite `src/mcp_server.py` — internal tools, per-module endpoints, context registration
  - Auto-generate `MCP_TOOL_DEFS` from `TOOLS` dict for 15 internal tools + `honcho_extract_facts`
  - Define `_INTERNAL_TOOL_NAMES` list with startup assertions against `TOOLS` and `_TOOL_HANDLERS`
  - Add `_module_tool_executor: dict[str, Callable]` module-level store
  - Add `POST /mcp/{module}/context` endpoint: receives JSON body with `workspace_name`, `observer`, `observed`, `session_name`, etc., calls `create_tool_executor()`, stores result in `_module_tool_executor[module]`
  - Add `DELETE /mcp/{module}/context` endpoint: removes `_module_tool_executor[module]`
  - Add `POST /mcp/{module}` endpoint: JSON-RPC handler for `initialize`, `tools/list`, `tools/call`
  - `dispatch_tool(name, args, module)`: reads `_module_tool_executor[module]`, calls `tool_executor(name, args)` for internal tools; extraction store logic for `honcho_extract_facts`
  - Keep `GET /mcp/health` and `GET /mcp/extraction` unchanged
  - Remove all HTTP-based dispatch (`_api()`, `httpx` calls to REST API)
  - Remove `chat`, `get_peer_context`, and all upstream-MCP tool names
  - _Requirements: 2.1–2.12_

- [x] 5. Rewrite `src/utils/acp_provider.py` — remove TOOL_NAME_MAP, add HTTP context registration
  - Delete `TOOL_NAME_MAP` dict entirely
  - In `build_agentic_prompt()`, use `name` directly instead of `TOOL_NAME_MAP.get(name, name)`
  - Accept `tool_executor: Callable | None = None` param in `honcho_llm_call_inner_acp()`
  - Add `_register_mcp_context(module, params)` helper: POSTs to `http://localhost:8000/mcp/{module}/context`
  - Add `_deregister_mcp_context(module)` helper: DELETEs `http://localhost:8000/mcp/{module}/context`
  - Before gateway call: read `tool_executor._ctx` to extract serializable params, call `_register_mcp_context()`
  - After gateway call (in `finally`): call `_deregister_mcp_context()`
  - Remove `_module_tool_executor` dict if present from previous implementation (context now managed via HTTP)
  - _Requirements: 2.10, 2.11_

- [x] 6. Verify tests pass
  - Run `uv run pytest tests/test_mcp_bug_condition.py tests/test_mcp_preservation.py -v --noconftest`
  - Update tests if needed to match the HTTP context registration pattern (tests may need to register context before calling tools)
  - All tests should pass
  - _Requirements: 2.1–2.12, 3.1–3.8_

- [ ] 7. Restore client-facing TypeScript MCP (honcho-mcp)

  - [ ] 7.1 Restore `mcp/` directory from `main` branch
    - Run `git checkout main -- mcp/` to restore all original TS MCP files
    - Verify `server.ts`, `types.ts`, `tools/*.ts` are identical to `main`
    - _Requirements: 2.14_

  - [ ] 7.2 Replace `mcp/src/index.ts` — Node.js HTTP server with StreamableHTTPServerTransport
    - Remove Cloudflare Workers entry point
    - Listen on port from `MCP_PORT` env var (default 8001)
    - _Requirements: 2.14_

  - [ ] 7.3 Update `mcp/src/config.ts` — read config from env vars
    - Replace request header parsing with env var reads (`MCP_WORKSPACE_ID`, `HONCHO_BASE_URL`, `HONCHO_API_KEY`)
    - `parseConfig()` no longer takes a `Request` parameter
    - _Requirements: 2.14_

  - [ ] 7.4 Update `Dockerfile` — add Node.js build stage
    - Add `node:22-slim` build stage for TS MCP compilation
    - Add Node.js runtime to final image
    - Copy built dist and node_modules
    - _Requirements: 2.14_

  - [ ] 7.5 Update `docker/entrypoint.sh` — start TS MCP process
    - Add `node /app/mcp/dist/index.js &` before FastAPI server
    - _Requirements: 2.14_

  - [ ] 7.6 Verify TS MCP compiles and responds
    - Run `npx tsc` in `mcp/` — zero errors
    - Test `initialize`, `tools/list` return expected responses
    - _Requirements: 2.14_

- [ ] 8. Checkpoint — Ensure all tests pass
  - Run all tests, verify no regressions
  - Ask the user if questions arise

- [ ]* 9. Data cleanup — Wipe Honcho database
  - Run `docker compose down -v` to remove the PostgreSQL volume
  - Honcho auto-runs `alembic upgrade head` on next startup, recreating a clean schema
  - _Requirements: 2.13_
