"""
Bug Condition Exploration Test — MCP Internal Tools Migration

This test encodes the EXPECTED (correct) behavior. On unfixed code, these tests
MUST FAIL — failure confirms the bug exists. After the fix is applied, these
tests should PASS.

Bug Condition: MCP_TOOL_DEFS names/schemas don't match internal TOOLS;
dispatch uses HTTP instead of _TOOL_HANDLERS.

NOTE: Due to circular imports in the codebase (agent_tools → crud → dreamer →
specialists → agent_tools), we cannot import src.mcp_server or src.utils.agent_tools
directly. Instead, we use isolated module loading (same pattern as
test_mcp_preservation.py) to load mcp_server.py with stub imports and inspect
the runtime MCP_TOOL_DEFS. For the internal TOOLS dict, we parse agent_tools.py
source using AST (TOOLS is a static dict with only literals, so ast.literal_eval
works). For dispatch_tool and TOOL_NAME_MAP checks, we use AST-based source
inspection.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10
"""

import ast
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths to source files (relative to project root)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_SERVER_PATH = _PROJECT_ROOT / "src" / "mcp_server.py"
_AGENT_TOOLS_PATH = _PROJECT_ROOT / "src" / "utils" / "agent_tools.py"
_ACP_PROVIDER_PATH = _PROJECT_ROOT / "src" / "utils" / "acp_provider.py"

# The 15 internal tool names that MUST be in MCP_TOOL_DEFS after the fix
_INTERNAL_TOOL_NAMES = [
    "create_observations",
    "delete_observations",
    "update_peer_card",
    "search_memory",
    "search_messages",
    "grep_messages",
    "get_messages_by_date_range",
    "search_messages_temporal",
    "get_observation_context",
    "get_recent_observations",
    "get_most_derived_observations",
    "get_peer_card",
    "finish_consolidation",
    "extract_preferences",
    "get_reasoning_chain",
]

# Upstream-MCP tools that should NOT be in MCP_TOOL_DEFS after the fix
_REMOVED_TOOL_NAMES = [
    "chat",
    "get_peer_context",
    "list_conclusions",
    "query_conclusions",
    "create_conclusions",
    "delete_conclusion",
    "set_peer_card",
]


# ---------------------------------------------------------------------------
# Helpers: extract TOOLS dict from agent_tools.py source via AST
# ---------------------------------------------------------------------------


def _extract_tools_dict_from_source() -> dict[str, dict[str, Any]]:
    """Parse agent_tools.py and extract the TOOLS dict by exec-ing just the
    assignment in a minimal namespace.

    TOOLS is a static dict with only Python literals and string concatenation
    (no function calls or variable references), so exec in an empty namespace
    works. We use AST to locate the exact source lines of the TOOLS assignment,
    then exec just that snippet.
    """
    source = _AGENT_TOOLS_PATH.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "TOOLS" and node.value is not None:
                lines = source.splitlines()
                start_line = node.lineno - 1  # assignment starts here
                end_line = node.value.end_lineno  # dict value ends here
                snippet = "\n".join(lines[start_line:end_line])
                # Execute the snippet in a minimal namespace
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
        handlers_section = source[match.start() :]
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
        handlers_block = handlers_section[: end_idx + 1]
        for m in re.finditer(r'"([a-z_]+)"\s*:', handlers_block):
            keys.add(m.group(1))
    return keys


# ---------------------------------------------------------------------------
# Isolated module loading: load mcp_server.py with stub imports but real
# TOOLS data so MCP_TOOL_DEFS is auto-generated at runtime.
# (Same pattern as test_mcp_preservation.py)
# ---------------------------------------------------------------------------


def _load_mcp_server_isolated() -> types.ModuleType:
    """Load mcp_server.py in an isolated way that avoids circular imports.

    We create stubs for src.utils.agent_tools and src.config, injecting the
    real TOOLS dict (parsed from source) and _TOOL_HANDLERS keys (as dummy
    callables) so that MCP_TOOL_DEFS is correctly auto-generated at runtime.
    """
    source = _MCP_SERVER_PATH.read_text()

    # Parse the real TOOLS dict from agent_tools.py source
    tools_dict = _extract_tools_dict_from_source()
    handler_keys = _extract_tool_handlers_keys_from_source()

    # Build a dummy _TOOL_HANDLERS with the right keys (values are just stubs)
    dummy_handlers = {k: lambda ctx, args: None for k in handler_keys}

    # Stub for src.utils.agent_tools — with real TOOLS data
    agent_tools_stub = types.ModuleType("src.utils.agent_tools")
    agent_tools_stub.ToolContext = type("ToolContext", (), {})
    agent_tools_stub._TOOL_HANDLERS = dummy_handlers
    agent_tools_stub.TOOLS = tools_dict

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
        mod = types.ModuleType("src.mcp_server")
        mod.__file__ = str(_MCP_SERVER_PATH)
        exec(compile(source, str(_MCP_SERVER_PATH), "exec"), mod.__dict__)
        return mod
    finally:
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


# ---------------------------------------------------------------------------
# Load modules once at module level
# ---------------------------------------------------------------------------

_tools_dict = _extract_tools_dict_from_source()
_mcp_mod = _load_mcp_server_isolated()

# Runtime MCP_TOOL_DEFS from the loaded module
_MCP_TOOL_DEFS: list[dict[str, Any]] = _mcp_mod.MCP_TOOL_DEFS

# Build name → tool def dict for quick lookup
_mcp_tool_by_name: dict[str, dict[str, Any]] = {t["name"]: t for t in _MCP_TOOL_DEFS}

# Set of all MCP tool names
_mcp_tool_names: set[str] = set(_mcp_tool_by_name.keys())


# ---------------------------------------------------------------------------
# Test Classes
# ---------------------------------------------------------------------------


class TestMCPToolDefsContainInternalTools:
    """MCP_TOOL_DEFS must contain a tool entry for each of the 15 internal tool names."""

    @pytest.mark.parametrize("tool_name", _INTERNAL_TOOL_NAMES)
    def test_mcp_tool_defs_has_internal_tool(self, tool_name: str):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9**"""
        assert tool_name in _mcp_tool_names, (
            f"MCP_TOOL_DEFS is missing internal tool '{tool_name}'. "
            f"Found: {sorted(_mcp_tool_names)}"
        )


class TestMCPToolDefsSchemasMatchInternal:
    """MCP_TOOL_DEFS tool schemas must match TOOLS[name]['input_schema'] for each internal tool."""

    @pytest.mark.parametrize("tool_name", _INTERNAL_TOOL_NAMES)
    def test_mcp_schema_matches_internal(self, tool_name: str):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9**"""
        mcp_tool = _mcp_tool_by_name.get(tool_name)
        if mcp_tool is None:
            pytest.fail(
                f"MCP_TOOL_DEFS has no tool named '{tool_name}' — cannot compare schema"
            )

        mcp_schema = mcp_tool.get("inputSchema")
        internal_schema = _tools_dict.get(tool_name, {}).get("input_schema")

        assert internal_schema is not None, (
            f"TOOLS dict has no entry for '{tool_name}' — cannot compare schema"
        )

        assert mcp_schema == internal_schema, (
            f"Schema mismatch for '{tool_name}'.\n"
            f"  MCP inputSchema: {mcp_schema}\n"
            f"  Internal input_schema: {internal_schema}"
        )


class TestDispatchToolRoutesToHandlers:
    """dispatch_tool for any internal tool name must route to _TOOL_HANDLERS, not HTTP."""

    def test_dispatch_tool_does_not_use_http(self):
        """**Validates: Requirements 1.10**

        Inspect the source code of dispatch_tool to verify it dispatches
        internal tools via _TOOL_HANDLERS rather than HTTP REST API calls.
        """
        source = _MCP_SERVER_PATH.read_text()

        # Extract the dispatch_tool function body
        tree = ast.parse(source)
        dispatch_func_source = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "dispatch_tool":
                    start_line = node.lineno - 1
                    end_line = node.end_lineno if node.end_lineno else start_line + 1
                    lines = source.splitlines()
                    dispatch_func_source = "\n".join(lines[start_line:end_line])
                    break

        assert dispatch_func_source is not None, (
            "dispatch_tool function not found in mcp_server.py"
        )

        # Check that dispatch_tool references _TOOL_HANDLERS for routing
        uses_handlers = "_TOOL_HANDLERS" in dispatch_func_source

        # Check that dispatch_tool does NOT use httpx / _api for internal tools
        uses_http = "_api(" in dispatch_func_source or "httpx" in dispatch_func_source

        assert uses_handlers and not uses_http, (
            f"dispatch_tool should route internal tools via _TOOL_HANDLERS (not HTTP).\n"
            f"  References _TOOL_HANDLERS: {uses_handlers}\n"
            f"  Uses HTTP (_api/httpx): {uses_http}"
        )


class TestToolNameMapNotUsedInPrompt:
    """TOOL_NAME_MAP must NOT be used in build_agentic_prompt (tool names pass through as-is)."""

    def test_tool_name_map_not_used_in_build_agentic_prompt(self):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9**"""
        source = _ACP_PROVIDER_PATH.read_text()

        # Extract the build_agentic_prompt function body
        tree = ast.parse(source)
        func_source = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "build_agentic_prompt":
                    start_line = node.lineno - 1
                    end_line = node.end_lineno if node.end_lineno else start_line + 1
                    lines = source.splitlines()
                    func_source = "\n".join(lines[start_line:end_line])
                    break

        assert func_source is not None, (
            "build_agentic_prompt function not found in acp_provider.py"
        )

        assert "TOOL_NAME_MAP" not in func_source, (
            "build_agentic_prompt still references TOOL_NAME_MAP. "
            "Tool names should pass through as-is without mapping."
        )


class TestMCPToolDefsExcludesRemovedTools:
    """MCP_TOOL_DEFS must NOT contain removed upstream-MCP tools."""

    @pytest.mark.parametrize("removed_name", _REMOVED_TOOL_NAMES)
    def test_mcp_tool_defs_excludes_removed_tool(self, removed_name: str):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10**"""
        assert removed_name not in _mcp_tool_names, (
            f"MCP_TOOL_DEFS still contains removed upstream-MCP tool '{removed_name}'. "
            f"This tool should have been removed."
        )
