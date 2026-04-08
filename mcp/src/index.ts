/**
 * Standalone HTTP MCP server for Honcho.
 * Uses the same simple JSON-RPC pattern as babyclaw-mcp (no Streamable HTTP transport).
 *
 * Endpoints:
 *   POST /mcp         — JSON-RPC MCP protocol (initialize, tools/list, tools/call)
 *   GET  /extraction  — Pop extraction result (for ACP provider)
 *   GET  /health      — Health check
 */

import express from "express";
import { Honcho } from "@honcho-ai/sdk";

const PORT = parseInt(process.env.MCP_PORT || "3100", 10);
const HONCHO_BASE_URL = process.env.HONCHO_BASE_URL || "http://localhost:8000";
const HONCHO_API_KEY = process.env.HONCHO_API_KEY || "local";
const HONCHO_WORKSPACE_ID = process.env.HONCHO_WORKSPACE_ID || "default";

const honcho = new Honcho({
  apiKey: HONCHO_API_KEY,
  baseURL: HONCHO_BASE_URL,
  workspaceId: HONCHO_WORKSPACE_ID,
});

// ─── Extraction Store ────────────────────────────────────────────────────────
let lastExtractionResult: string | null = null;

// ─── Tool Definitions ────────────────────────────────────────────────────────
const TOOL_DEFS = [
  {
    name: "list_conclusions",
    description: "List conclusions (facts/observations) about a peer.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      target_peer_id: { type: "string", description: "Optional target peer." },
    }, required: ["peer_id"] },
  },
  {
    name: "query_conclusions",
    description: "Semantic search across conclusions ranked by relevance.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      query: { type: "string", description: "Semantic search query." },
      target_peer_id: { type: "string", description: "Optional target peer." },
      top_k: { type: "number", description: "Max results (default: 5)." },
    }, required: ["peer_id", "query"] },
  },
  {
    name: "create_conclusions",
    description: "Create conclusions (facts/observations) about a peer.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      target_peer_id: { type: "string", description: "The peer the conclusions are about." },
      conclusions: { type: "array", items: { type: "string" }, description: "Conclusion content strings." },
      session_id: { type: "string", description: "Optional session ID." },
    }, required: ["peer_id", "target_peer_id", "conclusions"] },
  },
  {
    name: "delete_conclusion",
    description: "Delete a specific conclusion by ID.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      target_peer_id: { type: "string", description: "The target peer." },
      conclusion_id: { type: "string", description: "The conclusion to delete." },
    }, required: ["peer_id", "target_peer_id", "conclusion_id"] },
  },
  {
    name: "chat",
    description: "Ask Honcho a question about a peer using the reasoning system.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The peer to query about." },
      query: { type: "string", description: "Natural-language question." },
      target_peer_id: { type: "string", description: "Optional target peer." },
      reasoning_level: { type: "string", description: "'minimal','low','medium','high','max'" },
    }, required: ["peer_id", "query"] },
  },
  {
    name: "get_peer_card",
    description: "Get the peer card — compact biographical facts about a peer.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      target_peer_id: { type: "string", description: "Optional target peer." },
    }, required: ["peer_id"] },
  },
  {
    name: "set_peer_card",
    description: "Set or update the peer card for a peer.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      peer_card: { type: "array", items: { type: "string" }, description: "Fact strings." },
      target_peer_id: { type: "string", description: "Optional target peer." },
    }, required: ["peer_id", "peer_card"] },
  },
  {
    name: "get_peer_context",
    description: "Get comprehensive context for a peer — representation + peer card.",
    inputSchema: { type: "object", properties: {
      peer_id: { type: "string", description: "The observer peer." },
      target_peer_id: { type: "string", description: "Optional target peer." },
      search_query: { type: "string", description: "Optional semantic search to filter conclusions." },
      max_conclusions: { type: "number", description: "Max conclusions to include." },
    }, required: ["peer_id"] },
  },
  {
    name: "honcho_get_reasoning_chain",
    description: "Traverse the reasoning chain for a conclusion by following source_ids recursively.",
    inputSchema: { type: "object", properties: {
      conclusion_id: { type: "string", description: "The conclusion ID to start from." },
    }, required: ["conclusion_id"] },
  },
  {
    name: "honcho_extract_facts",
    description: "Submit extracted facts from conversation messages. Call this with the observations you extracted.",
    inputSchema: { type: "object", properties: {
      explicit: {
        type: "array", description: "Array of explicit observations",
        items: { type: "object", properties: { content: { type: "string" } }, required: ["content"] },
      },
    }, required: ["explicit"] },
  },
];

// ─── Tool Dispatch ───────────────────────────────────────────────────────────

function textResult(data: unknown) {
  return { content: [{ type: "text", text: typeof data === "string" ? data : JSON.stringify(data) }] };
}

function errorResult(msg: string) {
  return { content: [{ type: "text", text: msg }], isError: true };
}

async function dispatchTool(name: string, args: Record<string, unknown>): Promise<unknown> {
  try {
    switch (name) {
      case "list_conclusions": {
        const peer = await honcho.peer(args.peer_id as string);
        const scope = args.target_peer_id ? peer.conclusionsOf(args.target_peer_id as string) : peer.conclusions;
        const page = await scope.list();
        return textResult(page.items.map((c) => ({ id: c.id, content: c.content, observer_id: c.observerId, observed_id: c.observedId, created_at: c.createdAt })));
      }
      case "query_conclusions": {
        const peer = await honcho.peer(args.peer_id as string);
        const scope = args.target_peer_id ? peer.conclusionsOf(args.target_peer_id as string) : peer.conclusions;
        const items = await scope.query(args.query as string, args.top_k as number | undefined);
        return textResult(items.map((c) => ({ id: c.id, content: c.content, observer_id: c.observerId, observed_id: c.observedId, created_at: c.createdAt })));
      }
      case "create_conclusions": {
        const peer = await honcho.peer(args.peer_id as string);
        const scope = peer.conclusionsOf(args.target_peer_id as string);
        const conclusions = args.conclusions as string[];
        await scope.create(conclusions.map((content) => ({ content, sessionId: args.session_id as string | undefined })));
        return textResult(`Created ${conclusions.length} conclusion(s)`);
      }
      case "delete_conclusion": {
        const peer = await honcho.peer(args.peer_id as string);
        const scope = peer.conclusionsOf(args.target_peer_id as string);
        await scope.delete(args.conclusion_id as string);
        return textResult("Conclusion deleted");
      }
      case "chat": {
        const peer = await honcho.peer(args.peer_id as string);
        const result = await peer.chat(args.query as string, {
          target: args.target_peer_id as string | undefined,
          reasoningLevel: args.reasoning_level as string | undefined,
        });
        return textResult(result ?? "None");
      }
      case "get_peer_card": {
        const peer = await honcho.peer(args.peer_id as string);
        const card = await peer.getCard(args.target_peer_id as string | undefined);
        return textResult(card ?? "No peer card found.");
      }
      case "set_peer_card": {
        const peer = await honcho.peer(args.peer_id as string);
        await peer.setCard(args.peer_card as string[], args.target_peer_id as string | undefined);
        return textResult("Peer card updated");
      }
      case "get_peer_context": {
        const peer = await honcho.peer(args.peer_id as string);
        const context = await peer.context({
          target: args.target_peer_id as string | undefined,
          searchQuery: args.search_query as string | undefined,
          maxConclusions: args.max_conclusions as number | undefined,
        });
        return textResult({ peer_id: context.peerId, target_id: context.targetId, representation: context.representation, peer_card: context.peerCard });
      }
      case "honcho_get_reasoning_chain": {
        const chain: unknown[] = [];
        const visited = new Set<string>();
        const queue = [args.conclusion_id as string];
        while (queue.length > 0) {
          const currentId = queue.shift()!;
          if (visited.has(currentId)) continue;
          visited.add(currentId);
          try {
            const resp = await fetch(`${HONCHO_BASE_URL}/v3/workspaces/${HONCHO_WORKSPACE_ID}/conclusions/${currentId}`);
            if (!resp.ok) { chain.push({ id: currentId, error: "Not found" }); continue; }
            const c = await resp.json() as Record<string, unknown>;
            chain.push(c);
            const srcIds = c.source_ids as string[] | undefined;
            if (srcIds?.length) { for (const s of srcIds) if (!visited.has(s)) queue.push(s); }
          } catch { chain.push({ id: currentId, error: "Not found" }); }
        }
        return textResult({ chain });
      }
      case "honcho_extract_facts": {
        const explicit = args.explicit as Array<{ content: string }>;
        if (!explicit?.length) return errorResult("Missing explicit");
        lastExtractionResult = JSON.stringify({ explicit });
        return textResult({ stored: explicit.length });
      }
      default:
        return errorResult(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return errorResult(err instanceof Error ? err.message : String(err));
  }
}

// ─── Express App ─────────────────────────────────────────────────────────────

const app = express();
app.use(express.json({ limit: "10mb" }));

app.post("/mcp", async (req, res) => {
  try {
    const msg = req.body;
    if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") {
      res.status(400).json({ jsonrpc: "2.0", id: null, error: { code: -32600, message: "Invalid JSON-RPC" } });
      return;
    }
    const { method, id, params } = msg;
    switch (method) {
      case "initialize":
        res.json({ jsonrpc: "2.0", id, result: {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "honcho-mcp", version: "3.0.0" },
        }});
        return;
      case "notifications/initialized":
        res.status(204).end();
        return;
      case "tools/list":
        res.json({ jsonrpc: "2.0", id, result: { tools: TOOL_DEFS } });
        return;
      case "tools/call": {
        const toolName = params?.name;
        const toolArgs = params?.arguments || {};
        if (!toolName) { res.json({ jsonrpc: "2.0", id, error: { code: -32602, message: "Missing tool name" } }); return; }
        const result = await dispatchTool(toolName, toolArgs);
        res.json({ jsonrpc: "2.0", id, result });
        return;
      }
      default:
        res.json({ jsonrpc: "2.0", id, error: { code: -32601, message: `Unknown method: ${method}` } });
    }
  } catch (err) {
    res.status(500).json({ jsonrpc: "2.0", id: null, error: { code: -32603, message: err instanceof Error ? err.message : String(err) } });
  }
});

app.get("/extraction", (_req, res) => {
  const result = lastExtractionResult;
  lastExtractionResult = null;
  if (result) { res.json({ text: result }); }
  else { res.status(404).json({ error: "No extraction result" }); }
});

app.get("/health", (_req, res) => { res.json({ status: "ok" }); });

app.listen(PORT, "0.0.0.0", () => { console.log(`Honcho MCP server listening on port ${PORT}`); });
