import { createServer as createHttpServer, IncomingMessage, ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { parseConfig, createClient } from "./config.js";
import { createServer } from "./server.js";

const PORT = parseInt(process.env.MCP_PORT || "8001", 10);

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
};

function setCorsHeaders(res: ServerResponse): void {
  for (const [key, value] of Object.entries(CORS_HEADERS)) {
    res.setHeader(key, value);
  }
}

// Per-session transports keyed by session ID
const transports = new Map<string, StreamableHTTPServerTransport>();

async function handleMcp(req: IncomingMessage, res: ServerResponse): Promise<void> {
  setCorsHeaders(res);

  // CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  const sessionId = req.headers["mcp-session-id"] as string | undefined;

  if (req.method === "POST") {
    // Read body
    const chunks: Buffer[] = [];
    for await (const chunk of req) {
      chunks.push(chunk as Buffer);
    }
    const body = JSON.parse(Buffer.concat(chunks).toString());

    if (sessionId && transports.has(sessionId)) {
      // Existing session — reuse transport
      const transport = transports.get(sessionId)!;
      await transport.handleRequest(req, res, body);
      return;
    }

    // New session — must be an initialize request
    if (!sessionId && isInitializeRequest(body)) {
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (sid: string) => {
          transports.set(sid, transport);
        },
      });

      transport.onclose = () => {
        if (transport.sessionId) {
          transports.delete(transport.sessionId);
        }
      };

      const config = parseConfig();
      const honcho = createClient(config);
      const mcpServer = createServer({ honcho, config });
      await mcpServer.connect(transport);
      await transport.handleRequest(req, res, body);
      return;
    }

    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Bad request: no valid session" },
    }));
    return;
  }

  if (req.method === "GET") {
    // SSE stream for notifications
    if (sessionId && transports.has(sessionId)) {
      const transport = transports.get(sessionId)!;
      await transport.handleRequest(req, res);
      return;
    }
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "No active session" }));
    return;
  }

  if (req.method === "DELETE") {
    // Session termination
    if (sessionId && transports.has(sessionId)) {
      const transport = transports.get(sessionId)!;
      await transport.handleRequest(req, res);
      return;
    }
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "No active session" }));
    return;
  }

  res.writeHead(405, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Method not allowed" }));
}

const server = createHttpServer(async (req, res) => {
  try {
    await handleMcp(req, res);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Internal server error";
    if (!res.headersSent) {
      setCorsHeaders(res);
      res.writeHead(500, { "Content-Type": "application/json" });
    }
    if (!res.writableEnded) {
      res.end(JSON.stringify({ error: message }));
    }
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`Honcho MCP server listening on port ${PORT}`);
});

// Graceful shutdown
async function shutdown(): Promise<void> {
  server.close();
  for (const [, transport] of transports) {
    await transport.close();
  }
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
