# Honcho MCP Server — Usage Guide

## Overview

This MCP server exposes Honcho's memory tools over HTTP. It runs inside the Honcho Docker container and is registered as an MCP server with the CLI engine at session creation.

## Tool Usage

### Querying Memory

**`get_peer_context`** — Get the full picture of what Honcho knows about a peer:
```
peer_id: "agent-main-default"
target_peer_id: "slack-U04ABC123"
search_query: "what projects are they working on"
```

**`query_conclusions`** — Semantic search for specific observations:
```
peer_id: "agent-main-default"
query: "programming languages"
target_peer_id: "slack-U04ABC123"
top_k: 5
```

**`chat`** — Ask Honcho a natural-language question (LLM-powered):
```
peer_id: "agent-main-default"
query: "What does this user care about most?"
target_peer_id: "slack-U04ABC123"
reasoning_level: "medium"
```

### Managing Memory

**`create_conclusions`** — Manually add observations:
```
peer_id: "agent-main-default"
target_peer_id: "slack-U04ABC123"
conclusions: ["User prefers dark mode", "User works at a fintech startup"]
```

**`delete_conclusion`** — Remove an incorrect observation:
```
peer_id: "agent-main-default"
target_peer_id: "slack-U04ABC123"
conclusion_id: "abc123"
```

**`set_peer_card`** — Update biographical facts:
```
peer_id: "agent-main-default"
peer_card: ["Works at fintech startup in Austin", "Prefers TypeScript"]
target_peer_id: "slack-U04ABC123"
```

### Reasoning Chain

**`honcho_get_reasoning_chain`** — See how a conclusion was derived:
```
conclusion_id: "abc123"
```
Returns the full chain of conclusions from root to the given conclusion, following `source_ids` recursively.

### Structured Extraction (Internal)

**`honcho_extract_facts`** — Used by the deriver to return structured output. Not intended for direct use. The deriver prompt instructs the CLI engine to call this tool with extracted facts, which are then read by the ACP provider via the `/extraction` endpoint.

## Peer Naming Convention

Peers are named `{channelType}-{userId}`:
- `slack-U04ABC123` — Slack user
- `discord-981234567890` — Discord user
- `agent-main-default` — The Babyclaw agent

## Session Setup

Sessions are created with observation settings:
- User peer: `observe_me: true, observe_others: false` (Honcho observes this peer)
- Agent peer: `observe_me: false, observe_others: true` (this peer observes others)

This dual-peer model tells Honcho's deriver to build cross-peer representations — the agent observes the user.
