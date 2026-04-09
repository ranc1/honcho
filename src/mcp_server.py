"""
Python MCP Server for Honcho — runs inside the FastAPI process.

Exposes 16 tools via JSON-RPC MCP protocol at POST /mcp/{module}:

  15 internal tools auto-generated from agent_tools.TOOLS dict
  + honcho_extract_facts (custom extraction store logic)

Per-module endpoints: /mcp/dreamer, /mcp/dialectic, /mcp/deriver
Each endpoint reads the tool_executor closure from _module_tool_executor for tool dispatch.

Context registration: Before each ACP call, the calling process POSTs to
/mcp/{module}/context with serializable params. The MCP server creates a live
tool_executor closure via create_tool_executor() and stores it in _module_tool_executor.
After the ACP call completes, DELETE /mcp/{module}/context removes it.

The extraction store is a module-level variable. The deriver process
reads it via GET /mcp/extraction (HTTP, cross-process).
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.utils.agent_tools import TOOLS, _TOOL_HANDLERS, create_tool_executor

logger = logging.getLogger(__name__)

router = APIRouter()

# ─── Valid Modules ────────────────────────────────────────────────────────────

_VALID_MODULES = ("dreamer", "dialectic", "deriver")

# ─── Internal Tool Names ──────────────────────────────────────────────────────

_INTERNAL_TOOL_NAMES = [
    "create_observations",
    "update_peer_card",
    "search_memory",
    "get_observation_context",
    "search_messages",
    "grep_messages",
    "get_messages_by_date_range",
    "search_messages_temporal",
    "get_recent_observations",
    "get_most_derived_observations",
    "get_peer_card",
    "delete_observations",
    "finish_consolidation",
    "extract_preferences",
    "get_reasoning_chain",
]

# Startup assertions: fail fast if upstream removes a tool
for _name in _INTERNAL_TOOL_NAMES:
    assert _name in TOOLS, f"Internal tool '{_name}' not found in TOOLS dict"
    assert _name in _TOOL_HANDLERS, f"Internal tool '{_name}' not found in _TOOL_HANDLERS"


# ─── Per-Module Tool Executor Store ───────────────────────────────────────────
# Keyed by module name ("dreamer", "dialectic", "deriver").
# Set via POST /mcp/{module}/context before ACP gateway call,
# read by dispatch_tool() during tool calls,
# removed via DELETE /mcp/{module}/context after ACP call completes.
# Safe because ACP gateway serializes per-module (concurrency-1 FIFO queue).

_module_tool_executor: dict[str, Callable] = {}


# ─── Extraction Store ─────────────────────────────────────────────────────────

_extraction_result: str | None = None


def store_extraction_result(json_str: str) -> None:
    global _extraction_result
    _extraction_result = json_str


def pop_extraction_result() -> str | None:
    global _extraction_result
    result = _extraction_result
    _extraction_result = None
    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _text(data: Any) -> dict:
    text = data if isinstance(data, str) else json.dumps(data)
    return {"content": [{"type": "text", "text": text}]}


def _error(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ─── Tool Definitions (16 tools) ──────────────────────────────────────────────

# Auto-generate MCP tool definitions from the TOOLS dict for 15 internal tools
MCP_TOOL_DEFS: list[dict[str, Any]] = [
    {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
    for name in _INTERNAL_TOOL_NAMES
    for t in [TOOLS[name]]
]

# Append honcho_extract_facts manually (16th tool, custom logic)
MCP_TOOL_DEFS.append({
    "name": "honcho_extract_facts",
    "description": "Submit extracted facts from conversation messages. Call this with the observations you extracted.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "explicit": {
                "type": "array",
                "description": "Array of explicit observations",
                "items": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            },
        },
        "required": ["explicit"],
    },
})


# ─── Tool Dispatch ────────────────────────────────────────────────────────────

async def dispatch_tool(name: str, args: dict[str, Any], module: str) -> dict:
    """Dispatch a tool call to the appropriate handler."""
    try:
        if name in _TOOL_HANDLERS:
            tool_executor = _module_tool_executor.get(module)
            if tool_executor is None:
                return _error(
                    f"No tool executor for module '{module}' — tool called outside ACP flow"
                )
            result = await tool_executor(name, args)
            return _text(result)

        if name == "honcho_extract_facts":
            explicit = args.get("explicit", [])
            if not explicit:
                return _error("Missing explicit")
            store_extraction_result(json.dumps({"explicit": explicit}))
            return _text({"stored": len(explicit)})

        return _error(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return _error(str(e))


# ─── Context Registration Endpoints ──────────────────────────────────────────

@router.post("/mcp/{module}/context")
async def register_context(request: Request, module: str) -> JSONResponse:
    """Register tool execution context for a module before ACP call.

    Receives serializable params, calls create_tool_executor() to build a live
    closure in this process, and stores it in _module_tool_executor[module].
    """
    if module not in _VALID_MODULES:
        return JSONResponse(
            status_code=404, content={"error": f"Unknown module: {module}"}
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body"}
        )

    try:
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
    except KeyError as e:
        return JSONResponse(
            status_code=400, content={"error": f"Missing required field: {e}"}
        )
    except Exception as e:
        logger.error(f"Failed to create tool executor for {module}: {e}")
        return JSONResponse(
            status_code=500, content={"error": str(e)}
        )


@router.delete("/mcp/{module}/context")
async def deregister_context(request: Request, module: str) -> JSONResponse:
    """Remove tool execution context after ACP call completes."""
    _module_tool_executor.pop(module, None)
    return JSONResponse({"status": "ok"})


# ─── Per-Module MCP JSON-RPC Endpoint ─────────────────────────────────────────

@router.post("/mcp/{module}")
async def mcp_module_endpoint(request: Request, module: str) -> JSONResponse:
    """Handle MCP JSON-RPC requests for a specific module (dreamer, dialectic, deriver)."""
    if module not in _VALID_MODULES:
        return JSONResponse(
            status_code=404, content={"error": f"Unknown module: {module}"}
        )

    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC"}},
            status_code=400,
        )

    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "honcho-mcp", "version": "3.0.0"},
        }})

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOL_DEFS}})

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if not tool_name:
            return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32602, "message": "Missing tool name"}})
        result = await dispatch_tool(tool_name, tool_args, module)
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})

    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}})


@router.get("/mcp/health")
async def mcp_health():
    return {"status": "ok"}


@router.get("/mcp/extraction")
async def mcp_extraction():
    """Pop extraction result — consumed on read by the deriver process."""
    result = pop_extraction_result()
    if result:
        return {"text": result}
    return JSONResponse(status_code=404, content={"error": "No extraction result"})
