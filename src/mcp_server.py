"""
Python MCP Server for Honcho — runs inside the FastAPI process.

Exposes 10 tools via JSON-RPC MCP protocol at POST /mcp:

  Conclusions: list_conclusions, query_conclusions, create_conclusions, delete_conclusion
  Peers:       chat, get_peer_card, set_peer_card, get_peer_context
  Custom:      honcho_get_reasoning_chain, honcho_extract_facts

Uses Honcho's REST API (localhost, same container) for all tool calls.

The extraction store is a module-level variable. The deriver process
reads it via GET /mcp/extraction (HTTP, cross-process).
"""

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config import settings
from src.utils.agent_tools import ToolContext, _TOOL_HANDLERS

logger = logging.getLogger(__name__)

router = APIRouter()

# ─── Configuration ────────────────────────────────────────────────────────────

_HONCHO_BASE_URL = "http://localhost:8000"
_WORKSPACE_ID = os.environ.get("MCP_WORKSPACE_ID") or settings.NAMESPACE or "default"


async def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """HTTP request to Honcho REST API."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        return await getattr(client, method)(f"{_HONCHO_BASE_URL}{path}", **kwargs)


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


# ─── Tool Definitions (10 tools) ──────────────────────────────────────────────

MCP_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_conclusions",
        "description": "List conclusions (facts/observations) about a peer.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
        }, "required": ["peer_id"]},
    },
    {
        "name": "query_conclusions",
        "description": "Semantic search across conclusions ranked by relevance.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "query": {"type": "string", "description": "Semantic search query."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
            "top_k": {"type": "number", "description": "Max results (default: 5)."},
        }, "required": ["peer_id", "query"]},
    },
    {
        "name": "create_conclusions",
        "description": "Create conclusions (facts/observations) about a peer.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "target_peer_id": {"type": "string", "description": "The peer the conclusions are about."},
            "conclusions": {"type": "array", "items": {"type": "string"}, "description": "Conclusion content strings."},
            "session_id": {"type": "string", "description": "Optional session ID."},
        }, "required": ["peer_id", "target_peer_id", "conclusions"]},
    },
    {
        "name": "delete_conclusion",
        "description": "Delete a specific conclusion by ID.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "target_peer_id": {"type": "string", "description": "The target peer."},
            "conclusion_id": {"type": "string", "description": "The conclusion to delete."},
        }, "required": ["peer_id", "target_peer_id", "conclusion_id"]},
    },
    {
        "name": "chat",
        "description": "Ask Honcho a question about a peer using the reasoning system.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The peer to query about."},
            "query": {"type": "string", "description": "Natural-language question."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
            "reasoning_level": {"type": "string", "description": "'minimal','low','medium','high','max'"},
        }, "required": ["peer_id", "query"]},
    },
    {
        "name": "get_peer_card",
        "description": "Get the peer card — compact biographical facts about a peer.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
        }, "required": ["peer_id"]},
    },
    {
        "name": "set_peer_card",
        "description": "Set or update the peer card for a peer.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "peer_card": {"type": "array", "items": {"type": "string"}, "description": "Fact strings."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
        }, "required": ["peer_id", "peer_card"]},
    },
    {
        "name": "get_peer_context",
        "description": "Get comprehensive context for a peer — representation + peer card.",
        "inputSchema": {"type": "object", "properties": {
            "peer_id": {"type": "string", "description": "The observer peer."},
            "target_peer_id": {"type": "string", "description": "Optional target peer."},
            "search_query": {"type": "string", "description": "Optional semantic search to filter conclusions."},
            "max_conclusions": {"type": "number", "description": "Max conclusions to include."},
        }, "required": ["peer_id"]},
    },
    {
        "name": "honcho_get_reasoning_chain",
        "description": "Traverse the reasoning chain for a conclusion by following source_ids recursively.",
        "inputSchema": {"type": "object", "properties": {
            "conclusion_id": {"type": "string", "description": "The conclusion ID to start from."},
            "direction": {"type": "string", "description": "'premises', 'conclusions', or 'both' (default: 'both')."},
        }, "required": ["conclusion_id"]},
    },
    {
        "name": "honcho_extract_facts",
        "description": "Submit extracted facts from conversation messages. Call this with the observations you extracted.",
        "inputSchema": {"type": "object", "properties": {
            "explicit": {
                "type": "array", "description": "Array of explicit observations",
                "items": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
            },
        }, "required": ["explicit"]},
    },
]


# ─── Tool Dispatch ────────────────────────────────────────────────────────────

async def dispatch_tool(name: str, args: dict[str, Any]) -> dict:
    """Dispatch a tool call to the Honcho REST API."""
    try:
        wid = _WORKSPACE_ID

        if name == "list_conclusions":
            peer_id = args["peer_id"]
            target = args.get("target_peer_id")
            # POST /v3/workspaces/{wid}/conclusions/list with optional filters
            filters: dict[str, Any] = {"observer_id": peer_id}
            if target:
                filters["observed_id"] = target
            resp = await _api("post", f"/v3/workspaces/{wid}/conclusions/list",
                              json={"filters": filters})
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            return _text([{
                "id": c.get("id"), "content": c.get("content"),
                "observer_id": c.get("observer_id"), "observed_id": c.get("observed_id"),
                "created_at": c.get("created_at"),
            } for c in items])

        if name == "query_conclusions":
            peer_id = args["peer_id"]
            query = args["query"]
            target = args.get("target_peer_id")
            top_k = args.get("top_k")
            # POST /v3/workspaces/{wid}/conclusions/query
            body: dict[str, Any] = {"query": query}
            if top_k is not None:
                body["top_k"] = int(top_k)
            filters: dict[str, Any] = {"observer_id": peer_id}
            if target:
                filters["observed_id"] = target
            body["filters"] = filters
            resp = await _api("post", f"/v3/workspaces/{wid}/conclusions/query", json=body)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", []) if isinstance(data, dict) else []
            return _text([{
                "id": c.get("id"), "content": c.get("content"),
                "observer_id": c.get("observer_id"), "observed_id": c.get("observed_id"),
                "created_at": c.get("created_at"),
            } for c in items])

        if name == "create_conclusions":
            peer_id = args["peer_id"]
            target = args["target_peer_id"]
            conclusions = args["conclusions"]
            session_id = args.get("session_id")
            # POST /v3/workspaces/{wid}/conclusions
            body_items = []
            for c in conclusions:
                item: dict[str, Any] = {
                    "content": c,
                    "observer_id": peer_id,
                    "observed_id": target,
                }
                if session_id:
                    item["session_id"] = session_id
                body_items.append(item)
            resp = await _api("post", f"/v3/workspaces/{wid}/conclusions",
                              json={"conclusions": body_items})
            resp.raise_for_status()
            return _text(f"Created {len(conclusions)} conclusion(s)")

        if name == "delete_conclusion":
            conclusion_id = args["conclusion_id"]
            # DELETE /v3/workspaces/{wid}/conclusions/{id}
            resp = await _api("delete", f"/v3/workspaces/{wid}/conclusions/{conclusion_id}")
            resp.raise_for_status()
            return _text("Conclusion deleted")

        if name == "chat":
            peer_id = args["peer_id"]
            query = args["query"]
            target = args.get("target_peer_id")
            reasoning_level = args.get("reasoning_level")
            # POST /v3/workspaces/{wid}/peers/{pid}/chat
            body: dict[str, Any] = {"query": query}
            if target:
                body["target"] = target
            if reasoning_level:
                body["reasoning_level"] = reasoning_level
            resp = await _api("post", f"/v3/workspaces/{wid}/peers/{peer_id}/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
            return _text(data if isinstance(data, str) else data.get("content", data))

        if name == "get_peer_card":
            peer_id = args["peer_id"]
            target = args.get("target_peer_id")
            # GET /v3/workspaces/{wid}/peers/{pid}/card?target=...
            params: dict[str, str] = {}
            if target:
                params["target"] = target
            resp = await _api("get", f"/v3/workspaces/{wid}/peers/{peer_id}/card", params=params)
            resp.raise_for_status()
            data = resp.json()
            return _text(data if data else "No peer card found.")

        if name == "set_peer_card":
            peer_id = args["peer_id"]
            peer_card = args["peer_card"]
            target = args.get("target_peer_id")
            # PUT /v3/workspaces/{wid}/peers/{pid}/card?target=...
            params: dict[str, str] = {}
            if target:
                params["target"] = target
            resp = await _api("put", f"/v3/workspaces/{wid}/peers/{peer_id}/card",
                              params=params, json={"peer_card": peer_card})
            resp.raise_for_status()
            return _text("Peer card updated")

        if name == "get_peer_context":
            peer_id = args["peer_id"]
            target = args.get("target_peer_id")
            search_query = args.get("search_query")
            max_conclusions = args.get("max_conclusions")
            # GET /v3/workspaces/{wid}/peers/{pid}/context?target=...&search_query=...
            params: dict[str, str] = {}
            if target:
                params["target"] = target
            if search_query:
                params["search_query"] = search_query
            if max_conclusions is not None:
                params["max_conclusions"] = str(max_conclusions)
            resp = await _api("get", f"/v3/workspaces/{wid}/peers/{peer_id}/context", params=params)
            resp.raise_for_status()
            data = resp.json()
            return _text({
                "peer_id": data.get("peer_id"),
                "target_id": data.get("target_id"),
                "representation": data.get("representation"),
                "peer_card": data.get("peer_card"),
            })

        if name == "honcho_get_reasoning_chain":
            # Reuse Honcho's internal _handle_get_reasoning_chain directly (same process)
            handler = _TOOL_HANDLERS.get("get_reasoning_chain")
            if not handler:
                return _error("get_reasoning_chain handler not found")
            ctx = ToolContext(
                workspace_name=wid,
                observer="agent-main-default",
                observed="agent-main-default",
                session_name=None,
                current_messages=None,
                include_observation_ids=True,
                history_token_limit=10000,
                db_lock=asyncio.Lock(),
                configuration=None,
            )
            tool_input = {
                "observation_id": args["conclusion_id"],
                "direction": args.get("direction", "both"),
            }
            try:
                result = await handler(ctx, tool_input)
                return _text(result)
            except Exception as e:
                logger.error(f"get_reasoning_chain failed: {e}")
                return _error(str(e))

        if name == "honcho_extract_facts":
            explicit = args.get("explicit", [])
            if not explicit:
                return _error("Missing explicit")
            store_extraction_result(json.dumps({"explicit": explicit}))
            return _text({"stored": len(explicit)})

        return _error(f"Unknown tool: {name}")

    except httpx.HTTPStatusError as e:
        return _error(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return _error(str(e))


# ─── MCP JSON-RPC Endpoint ────────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """Handle MCP JSON-RPC requests."""
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
        result = await dispatch_tool(tool_name, tool_args)
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
