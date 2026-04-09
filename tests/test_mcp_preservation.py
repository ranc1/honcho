"""
Preservation Property Tests — MCP Internal Tools Migration

These tests capture BASELINE behavior that must NOT regress after the fix.
They MUST PASS on unfixed code.

Tests cover:
- Extraction store (store/pop semantics)
- HTTP endpoints (GET /mcp/health, GET /mcp/extraction)
- MCP JSON-RPC protocol (initialize, tools/list, notifications/initialized)
- honcho_extract_facts tool (stores extraction result)
- detect_module() classification logic

NOTE: Due to circular imports in the codebase, we cannot import src.mcp_server
directly. We use a combination of:
1. AST/source extraction for pure functions (store/pop)
2. FastAPI TestClient with the router for HTTP endpoints
3. Direct import for detect_module (no circular import issues)

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import ast
import importlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_SERVER_PATH = _PROJECT_ROOT / "src" / "mcp_server.py"
_AGENT_TOOLS_PATH = _PROJECT_ROOT / "src" / "utils" / "agent_tools.py"

# ---------------------------------------------------------------------------
# Helpers: extract TOOLS dict and _TOOL_HANDLERS keys from agent_tools.py
# (same approach as test_mcp_bug_condition.py)
# ---------------------------------------------------------------------------


def _extract_tools_dict_from_source() -> dict[str, dict[str, Any]]:
    """Parse agent_tools.py and extract the TOOLS dict by exec-ing just the
    assignment in a minimal namespace."""
    source = _AGENT_TOOLS_PATH.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "TOOLS" and node.value is not None:
                lines = source.splitlines()
                start_line = node.lineno - 1
                end_line = node.value.end_lineno
                snippet = "\n".join(lines[start_line:end_line])
                ns: dict[str, Any] = {"Any": Any}
                exec(compile(snippet, "<TOOLS>", "exec"), ns)
                return ns["TOOLS"]

    raise RuntimeError("Could not find TOOLS dict in agent_tools.py")


def _extract_tool_handlers_keys_from_source() -> set[str]:
    """Parse agent_tools.py and extract all keys from _TOOL_HANDLERS dict."""
    source = _AGENT_TOOLS_PATH.read_text()
    keys: set[str] = set()
    match = re.search(r"^_TOOL_HANDLERS\s*:", source, re.MULTILINE)
    if match:
        handlers_section = source[match.start():]
        brace_count = 0
        end_idx = 0
        for i, ch in enumerate(handlers_section):
            if ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i
                    break
        handlers_block = handlers_section[:end_idx + 1]
        for m in re.finditer(r'"([a-z_]+)"\s*:', handlers_block):
            keys.add(m.group(1))
    return keys


# ---------------------------------------------------------------------------
# Isolated module loading: extract store functions and router without
# triggering the full import chain (circular imports).
# ---------------------------------------------------------------------------


def _load_mcp_server_isolated():
    """
    Load mcp_server.py in an isolated way that avoids circular imports.

    We create a stub for src.utils.agent_tools and src.config, then exec
    the mcp_server module source. This gives us access to:
    - store_extraction_result / pop_extraction_result
    - router (FastAPI APIRouter with endpoints)
    - MCP_TOOL_DEFS
    - dispatch_tool
    """
    source = _MCP_SERVER_PATH.read_text()

    # Create stub modules to satisfy imports
    # Stub for src.utils.agent_tools — with real TOOLS data
    tools_dict = _extract_tools_dict_from_source()
    handler_keys = _extract_tool_handlers_keys_from_source()

    agent_tools_stub = types.ModuleType("src.utils.agent_tools")
    agent_tools_stub.ToolContext = type("ToolContext", (), {})
    agent_tools_stub.TOOLS = tools_dict
    agent_tools_stub._TOOL_HANDLERS = {k: lambda ctx, args: None for k in handler_keys}

    async def _stub_create_tool_executor(**kwargs):
        async def _stub_executor(name, args):
            return f"stub result for {name}"
        return _stub_executor

    agent_tools_stub.create_tool_executor = _stub_create_tool_executor

    # Stub for src.config
    config_stub = types.ModuleType("src.config")
    settings_obj = type("Settings", (), {"NAMESPACE": "default"})()
    config_stub.settings = settings_obj

    # Ensure parent modules exist
    if "src" not in sys.modules:
        src_mod = types.ModuleType("src")
        src_mod.__path__ = [str(_PROJECT_ROOT / "src")]
        sys.modules["src"] = src_mod
    if "src.utils" not in sys.modules:
        utils_mod = types.ModuleType("src.utils")
        utils_mod.__path__ = [str(_PROJECT_ROOT / "src" / "utils")]
        sys.modules["src.utils"] = utils_mod
    if "src.config" not in sys.modules:
        sys.modules["src.config"] = config_stub

    # Save and set stubs
    saved = {}
    for key in ["src.utils.agent_tools", "src.config"]:
        saved[key] = sys.modules.get(key)

    sys.modules["src.utils.agent_tools"] = agent_tools_stub
    sys.modules["src.config"] = config_stub

    try:
        # Create a fresh module and exec the source
        mod = types.ModuleType("src.mcp_server")
        mod.__file__ = str(_MCP_SERVER_PATH)
        exec(compile(source, str(_MCP_SERVER_PATH), "exec"), mod.__dict__)
        return mod
    finally:
        # Restore original modules
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


# Load once at module level
_mcp_mod = _load_mcp_server_isolated()

# Extract the functions and objects we need
store_extraction_result = _mcp_mod.store_extraction_result
pop_extraction_result = _mcp_mod.pop_extraction_result
router = _mcp_mod.router
MCP_TOOL_DEFS = _mcp_mod.MCP_TOOL_DEFS
dispatch_tool = _mcp_mod.dispatch_tool


# ---------------------------------------------------------------------------
# FastAPI TestClient setup
# ---------------------------------------------------------------------------

from fastapi import FastAPI
from starlette.testclient import TestClient

_app = FastAPI()
_app.include_router(router)
_client = TestClient(_app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_extraction_store():
    """Clear the extraction store to a known state."""
    _mcp_mod._extraction_result = None


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for non-empty JSON strings (valid extraction payloads)
json_payloads = st.dictionaries(
    keys=st.text(min_size=1, max_size=20, alphabet=st.characters(categories=("L", "N"))),
    values=st.one_of(
        st.text(min_size=0, max_size=100),
        st.integers(min_value=-1000, max_value=1000),
        st.booleans(),
    ),
    min_size=1,
    max_size=5,
).map(json.dumps)

# Strategy for explicit observations (what honcho_extract_facts expects)
explicit_observations = st.lists(
    st.fixed_dictionaries({"content": st.text(min_size=1, max_size=100)}),
    min_size=1,
    max_size=5,
)



# ===========================================================================
# Property 2.1: Extraction Store Semantics
# Validates: Requirement 3.1
# ===========================================================================


class TestExtractionStoreSemantics:
    """store_extraction_result(json_str) stores value, pop_extraction_result() returns and clears it."""

    @given(payload=json_payloads)
    @settings(max_examples=50)
    def test_store_then_pop_returns_stored_value(self, payload: str):
        """**Validates: Requirements 3.1**

        For any JSON string, storing it then popping returns the same string.
        """
        _reset_extraction_store()
        store_extraction_result(payload)
        result = pop_extraction_result()
        assert result == payload

    @given(payload=json_payloads)
    @settings(max_examples=50)
    def test_pop_clears_store(self, payload: str):
        """**Validates: Requirements 3.1**

        After popping, a second pop returns None (store is cleared).
        """
        _reset_extraction_store()
        store_extraction_result(payload)
        pop_extraction_result()  # first pop
        result = pop_extraction_result()  # second pop
        assert result is None

    def test_pop_empty_store_returns_none(self):
        """**Validates: Requirements 3.1**

        Popping from an empty store returns None.
        """
        _reset_extraction_store()
        assert pop_extraction_result() is None


# ===========================================================================
# Property 2.2: GET /mcp/health returns {"status": "ok"}
# Validates: Requirement 3.3
# ===========================================================================


class TestHealthEndpoint:
    """GET /mcp/health returns {"status": "ok"}."""

    def test_health_returns_ok(self):
        """**Validates: Requirements 3.3**"""
        resp = _client.get("/mcp/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ===========================================================================
# Property 2.3: GET /mcp/extraction returns stored extraction or 404
# Validates: Requirement 3.4
# ===========================================================================


class TestExtractionEndpoint:
    """GET /mcp/extraction returns stored extraction or 404."""

    @given(payload=json_payloads)
    @settings(max_examples=30)
    def test_extraction_returns_stored_value(self, payload: str):
        """**Validates: Requirements 3.4**

        When extraction store has a value, GET /mcp/extraction returns it
        with {"text": value} and 200 status.
        """
        _reset_extraction_store()
        store_extraction_result(payload)
        resp = _client.get("/mcp/extraction")
        assert resp.status_code == 200
        data = resp.json()
        assert "text" in data
        assert data["text"] == payload

    def test_extraction_returns_404_when_empty(self):
        """**Validates: Requirements 3.4**

        When extraction store is empty, GET /mcp/extraction returns 404.
        """
        _reset_extraction_store()
        resp = _client.get("/mcp/extraction")
        assert resp.status_code == 404

    @given(payload=json_payloads)
    @settings(max_examples=20)
    def test_extraction_consumes_on_read(self, payload: str):
        """**Validates: Requirements 3.4**

        GET /mcp/extraction pops the value (consumed on read).
        Second GET returns 404.
        """
        _reset_extraction_store()
        store_extraction_result(payload)
        resp1 = _client.get("/mcp/extraction")
        assert resp1.status_code == 200
        resp2 = _client.get("/mcp/extraction")
        assert resp2.status_code == 404


# ===========================================================================
# Property 2.4: MCP JSON-RPC initialize returns protocol version and server info
# Validates: Requirement 3.2
# ===========================================================================


class TestMCPInitialize:
    """MCP JSON-RPC initialize returns protocol version 2024-11-05 and server info."""

    @given(msg_id=st.one_of(st.integers(min_value=1, max_value=10000), st.text(min_size=1, max_size=10)))
    @settings(max_examples=30)
    def test_initialize_returns_protocol_version(self, msg_id):
        """**Validates: Requirements 3.2**

        For any valid JSON-RPC id, initialize returns protocolVersion 2024-11-05.
        """
        resp = _client.post("/mcp/dreamer", json={
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "initialize",
            "params": {},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == msg_id
        result = data["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "honcho-mcp"
        assert "version" in result["serverInfo"]


# ===========================================================================
# Property 2.5: MCP JSON-RPC tools/list returns a list of tool definitions
# Validates: Requirement 3.2
# ===========================================================================


class TestMCPToolsList:
    """MCP JSON-RPC tools/list returns a list of tool definitions."""

    def test_tools_list_returns_tools(self):
        """**Validates: Requirements 3.2**

        tools/list returns a non-empty list of tool definitions, each with
        name, description, and inputSchema.
        """
        resp = _client.post("/mcp/dreamer", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        tools = data["result"]["tools"]
        assert isinstance(tools, list)
        assert len(tools) > 0
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool


# ===========================================================================
# Property 2.6: MCP JSON-RPC notifications/initialized returns empty result
# Validates: Requirement 3.2
# ===========================================================================


class TestMCPNotificationsInitialized:
    """MCP JSON-RPC notifications/initialized returns empty result."""

    @given(msg_id=st.one_of(st.integers(min_value=1, max_value=10000), st.text(min_size=1, max_size=10)))
    @settings(max_examples=30)
    def test_notifications_initialized_returns_empty(self, msg_id):
        """**Validates: Requirements 3.2**

        notifications/initialized returns an empty result dict.
        """
        resp = _client.post("/mcp/dreamer", json={
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "notifications/initialized",
            "params": {},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == msg_id
        assert data["result"] == {}


# ===========================================================================
# Property 2.7: honcho_extract_facts tool stores extraction result
# Validates: Requirement 3.1
# ===========================================================================


class TestHonchoExtractFactsStoresResult:
    """honcho_extract_facts tool stores extraction result in module-level store."""

    @given(observations=explicit_observations)
    @settings(max_examples=30)
    def test_extract_facts_stores_in_extraction_store(self, observations: list):
        """**Validates: Requirements 3.1**

        Calling honcho_extract_facts via MCP tools/call stores the result
        in the extraction store, retrievable via GET /mcp/extraction.
        """
        _reset_extraction_store()

        resp = _client.post("/mcp/deriver", json={
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "honcho_extract_facts",
                "arguments": {"explicit": observations},
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        # The result should not be an error
        result = data["result"]
        assert result.get("isError") is not True

        # Now the extraction store should have the value
        extraction_resp = _client.get("/mcp/extraction")
        assert extraction_resp.status_code == 200
        extraction_data = extraction_resp.json()
        stored = json.loads(extraction_data["text"])
        assert stored["explicit"] == observations


# ===========================================================================
# Property 2.8: detect_module() classification logic
# Validates: Requirement 3.7
# ===========================================================================

from src.utils.acp_provider import detect_module


class TestDetectModule:
    """detect_module() classifies modules correctly based on tools and prompt content."""

    # --- Dreamer: has delete_observations or finish_consolidation in tools ---

    @given(extra_tools=st.lists(
        st.fixed_dictionaries({"name": st.text(min_size=1, max_size=20)}),
        min_size=0, max_size=3,
    ))
    @settings(max_examples=30)
    def test_dreamer_detected_by_delete_observations(self, extra_tools):
        """**Validates: Requirements 3.7**

        If tools contain 'delete_observations', detect_module returns 'dreamer'.
        """
        tools = [{"name": "delete_observations"}] + extra_tools
        result = detect_module([], tools)
        assert result == "dreamer"

    @given(extra_tools=st.lists(
        st.fixed_dictionaries({"name": st.text(min_size=1, max_size=20)}),
        min_size=0, max_size=3,
    ))
    @settings(max_examples=30)
    def test_dreamer_detected_by_finish_consolidation(self, extra_tools):
        """**Validates: Requirements 3.7**

        If tools contain 'finish_consolidation', detect_module returns 'dreamer'.
        """
        tools = [{"name": "finish_consolidation"}] + extra_tools
        result = detect_module([], tools)
        assert result == "dreamer"

    # --- Dialectic: has tools but no dreamer markers ---

    @given(tool_names=st.lists(
        st.text(min_size=1, max_size=20).filter(
            lambda n: n not in ("delete_observations", "finish_consolidation")
        ),
        min_size=1, max_size=5,
    ))
    @settings(max_examples=30)
    def test_dialectic_detected_with_tools_no_dreamer_markers(self, tool_names):
        """**Validates: Requirements 3.7**

        If tools are present but none are dreamer markers, detect_module returns 'dialectic'.
        """
        tools = [{"name": n} for n in tool_names]
        result = detect_module([], tools)
        assert result == "dialectic"

    # --- Deriver: no tools, keyword match ---

    @given(extra_text=st.text(min_size=0, max_size=50))
    @settings(max_examples=30)
    def test_deriver_detected_by_keywords(self, extra_text):
        """**Validates: Requirements 3.7**

        If no tools and prompt contains deriver keywords, detect_module returns 'deriver'.
        """
        messages = [{"content": f"extract explicit atomic facts {extra_text}", "role": "user"}]
        result = detect_module(messages, None)
        assert result == "deriver"

    # --- Summarizer: no tools, summarizer keywords or fallback ---

    # All keywords from other modules that could override summarizer detection
    _CONFLICTING_KEYWORDS = [
        "extract", "explicit", "atomic facts", "observation",
        "deductive", "inductive", "dream", "reasoning agent", "specialist",
        "query", "dialectic", "synthesis", "answer",
    ]

    @given(extra_text=st.text(min_size=0, max_size=50).filter(
        lambda t: not any(kw in t.lower() for kw in TestDetectModule._CONFLICTING_KEYWORDS)
    ))
    @settings(max_examples=30)
    def test_summarizer_detected_by_keywords(self, extra_text):
        """**Validates: Requirements 3.7**

        If no tools and prompt contains summarizer keywords, detect_module returns 'summarizer'.
        """
        messages = [{"content": f"summarize this conversation {extra_text}", "role": "user"}]
        result = detect_module(messages, None)
        assert result == "summarizer"

    def test_summarizer_fallback_no_keywords(self):
        """**Validates: Requirements 3.7**

        If no tools and no keyword matches, detect_module defaults to 'summarizer'.
        """
        messages = [{"content": "hello world xyz", "role": "user"}]
        result = detect_module(messages, None)
        assert result == "summarizer"
