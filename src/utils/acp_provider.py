"""
ACP Provider — Routes Honcho LLM calls through an external ACP gateway's HTTP bridge.

This module implements the same contract as other LLM providers in honcho_llm_call_inner().
Instead of calling an LLM API directly, it POSTs to the gateway's /api/v1/acp/prompt endpoint,
which routes the request through ACP to an LLM-backed engine process.

Two call patterns:
  1. Non-agentic (deriver, summarizer): Send prompt text, get text response back.
     For deriver: parse response into PromptRepresentation.
  2. Agentic (dreamer, dialectic): Construct combined prompt with system instructions,
     user content, and tool instruction section. The CLI engine handles tool calling
     via its registered MCP tools. Honcho's _execute_tool_loop is bypassed.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool name mapping: Honcho internal → MCP tool names
# ---------------------------------------------------------------------------

TOOL_NAME_MAP: dict[str, str] = {
    # Map Honcho internal tool names → Honcho canonical MCP tool names
    # (matching the official Honcho MCP server at honcho/mcp/src/tools/)
    "search_memory": "query_conclusions",
    "create_observations": "create_conclusions",
    "delete_observations": "delete_conclusion",
    "update_peer_card": "set_peer_card",
    "get_reasoning_chain": "honcho_get_reasoning_chain",
    "search_messages": "get_session_messages",
    "grep_messages": "get_session_messages",
    "get_recent_observations": "list_conclusions",
    "get_most_derived_observations": "list_conclusions",
    "get_observation_context": "query_conclusions",
    "get_messages_by_date_range": "get_session_messages",
    "search_messages_temporal": "get_session_messages",
    "get_peer_card": "get_peer_card",
    "finish_consolidation": "query_conclusions",
    "extract_preferences": "get_session_messages",
}

# ---------------------------------------------------------------------------
# Module detection from prompt content
# ---------------------------------------------------------------------------

MODULE_KEYWORDS: dict[str, list[str]] = {
    "deriver": ["extract", "explicit", "atomic facts", "observation"],
    "dreamer": ["deductive", "inductive", "dream", "reasoning agent", "specialist"],
    "dialectic": ["query", "dialectic", "synthesis", "answer"],
    "summarizer": ["summarize", "summary", "recap", "concise"],
}


def detect_module(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
    """Detect which Honcho module is making this call based on prompt content and tools."""
    if tools and len(tools) > 0:
        # Agentic call — dreamer or dialectic
        tool_names = {t.get("name", "") for t in tools}
        if "delete_observations" in tool_names or "finish_consolidation" in tool_names:
            return "dreamer"
        return "dialectic"

    # Non-agentic — check prompt content
    prompt_text = ""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            prompt_text += content.lower() + " "

    for module, keywords in MODULE_KEYWORDS.items():
        if any(kw in prompt_text for kw in keywords):
            return module

    return "summarizer"  # default fallback


# ---------------------------------------------------------------------------
# Prompt construction for agentic calls
# ---------------------------------------------------------------------------


def build_agentic_prompt(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> str:
    """
    Construct a combined prompt for agentic calls (dreamer, dialectic).

    The prompt includes:
    1. System instructions (from system messages)
    2. User content (from user messages)
    3. Tool instruction section listing available MCP tools

    The CLI engine's LLM will use its registered MCP tools to accomplish the task.
    """
    parts: list[str] = []

    # Extract system and user messages
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle Anthropic-style content blocks
            text_parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = "\n".join(text_parts)

        if role == "system":
            parts.append(f"[System Instructions]\n{content}")
        elif role == "user":
            parts.append(f"[Task]\n{content}")

    # Add tool instruction section
    if tools:
        tool_section = "\n[Available Tools]\nYou have the following tools available. Use them as needed:\n"
        for tool in tools:
            name = tool.get("name", "")
            description = tool.get("description", "")
            # Map Honcho internal tool name to MCP tool name
            mcp_name = TOOL_NAME_MAP.get(name, name)
            input_schema = tool.get("input_schema", tool.get("parameters", {}))
            params_desc = ""
            if input_schema and "properties" in input_schema:
                param_names = list(input_schema["properties"].keys())
                required = input_schema.get("required", [])
                param_parts = []
                for pname in param_names:
                    pinfo = input_schema["properties"][pname]
                    pdesc = pinfo.get("description", "")
                    req_marker = " (required)" if pname in required else ""
                    param_parts.append(f"    - {pname}{req_marker}: {pdesc}")
                params_desc = "\n".join(param_parts)

            tool_section += f"\n- {mcp_name}: {description}\n"
            if params_desc:
                tool_section += f"  Parameters:\n{params_desc}\n"

        parts.append(tool_section)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing for deriver
# ---------------------------------------------------------------------------


def parse_deriver_response(text: str, response_model: type[BaseModel]) -> BaseModel:
    """
    Parse a text response into a PromptRepresentation.

    Expects valid JSON from the honcho_extract_facts MCP tool result.
    If the response is not valid JSON, the extraction is skipped (empty result).
    """
    cleaned = text.strip()
    if not cleaned:
        logger.warning("Empty deriver response — extraction skipped")
        return response_model.model_validate({"explicit": []})

    try:
        data = json.loads(cleaned)
        result = response_model.model_validate(data)
        logger.info(f"Deriver response parsed successfully ({len(data.get('explicit', []))} explicit observations)")
        return result
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Deriver response is not valid JSON — extraction skipped. Error: {e}. Response preview: {cleaned[:200]}")
        return response_model.model_validate({"explicit": []})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@dataclass
class AcpLLMResponse:
    """Response from the ACP bridge, compatible with HonchoLLMCallResponse fields."""
    content: Any
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    finish_reasons: list[str] = field(default_factory=lambda: ["stop"])
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 1
    thinking_content: str | None = None
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)


async def honcho_llm_call_inner_acp(
    gateway_url: str,
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    response_model: type[BaseModel] | None = None,
    json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = False,
    timeout_ms: int = 120000,
) -> Any:
    """
    Route an LLM call through an external ACP gateway's HTTP bridge.

    For non-agentic calls (no tools): sends prompt text, gets text response.
    For agentic calls (with tools): constructs combined prompt with tool instructions,
    CLI engine handles tool calling via MCP tools.

    Returns an AcpLLMResponse that's compatible with HonchoLLMCallResponse.
    """
    # Detect which module is calling
    module = detect_module(messages, tools)

    # Build the prompt
    if tools and len(tools) > 0:
        # Agentic call — construct combined prompt
        prompt_text = build_agentic_prompt(messages, tools)
    else:
        # Non-agentic call — extract prompt from messages
        parts = []
        system_parts = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            if msg.get("role") == "system":
                system_parts.append(content)
            else:
                parts.append(content)
        prompt_text = "\n\n".join(parts)
        system_prompt = "\n\n".join(system_parts) if system_parts else None

    # Build the bridge request
    bridge_request: dict[str, Any] = {
        "module": module,
        "prompt": prompt_text,
    }

    # For deriver calls, instruct the CLI engine to use the honcho_extract_facts MCP tool
    if module == "deriver" and response_model is not None:
        bridge_request["prompt"] += (
            "\n\nIMPORTANT: You MUST submit your extracted facts by calling the "
            "honcho_extract_facts tool. Do NOT output JSON as text. "
            "Call honcho_extract_facts with your results. "
            "If there are no facts to extract, call honcho_extract_facts with an empty explicit array."
        )

    if tools and len(tools) > 0:
        # For agentic calls, system prompt is already embedded in the combined prompt
        pass
    elif system_parts:
        bridge_request["systemPrompt"] = "\n\n".join(system_parts)

    # POST to the gateway bridge
    bridge_url = f"{gateway_url}/api/v1/acp/prompt"
    timeout_s = timeout_ms / 1000.0

    logger.info(f"ACP provider: sending {module} prompt to {bridge_url} (timeout={timeout_s}s)")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            response = await client.post(bridge_url, json=bridge_request)

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"ACP bridge returned {response.status_code}: {error_body}")
            raise RuntimeError(f"ACP bridge error {response.status_code}: {error_body}")

        result = response.json()
        response_text = result.get("text", "")

    except httpx.TimeoutException:
        logger.error(f"ACP bridge timeout after {timeout_s}s for module {module}")
        raise RuntimeError(f"ACP bridge timeout after {timeout_s}s")
    except httpx.ConnectError as e:
        logger.error(f"ACP bridge connection failed: {e}")
        raise RuntimeError(f"ACP bridge connection failed: {e}")

    logger.info(f"ACP provider: received {len(response_text)} chars from {module}")

    # Parse response based on call type
    if response_model is not None:
        # Deriver call — check the MCP extraction store.
        # The CLI engine calls honcho_extract_facts MCP tool during the prompt,
        # which stores structured JSON in the FastAPI process's MCP endpoint.
        # The deriver runs in a separate process, so we read via HTTP.
        extraction_text = await _fetch_extraction_result()
        if extraction_text:
            logger.info(f"ACP provider: using tool-extracted result for {module} ({len(extraction_text)} chars)")
            parsed = parse_deriver_response(extraction_text, response_model)
        else:
            # Fallback: try parsing the raw text response
            parsed = parse_deriver_response(response_text, response_model)
        return AcpLLMResponse(content=parsed)

    # Plain text response (summarizer, agentic calls)
    return AcpLLMResponse(content=response_text)


async def _fetch_extraction_result() -> str | None:
    """
    Fetch extraction result from the MCP endpoint's extraction store.
    The MCP server runs in the FastAPI process (separate from the deriver).
    """
    url = "http://localhost:8000/mcp/extraction"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("text")
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch extraction result: {e}")
        return None
