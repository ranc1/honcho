# Honcho MCP Server

A standalone HTTP server implementing the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) for [Honcho](https://honcho.dev), providing AI memory and personalization tools to LLM clients.

This fork runs as a standalone Express server inside Docker (not Cloudflare Workers), alongside the Honcho API and deriver worker.

## Tools

| Tool | Description |
|---|---|
| `list_conclusions` | List conclusions (facts/observations) about a peer |
| `query_conclusions` | Semantic search across conclusions |
| `create_conclusions` | Create conclusions about a peer |
| `delete_conclusion` | Delete a conclusion by ID |
| `chat` | Ask Honcho a question about a peer (LLM-powered reasoning) |
| `get_peer_card` | Get biographical facts about a peer |
| `set_peer_card` | Update a peer's card |
| `get_peer_context` | Combined representation + peer card |
| `honcho_get_reasoning_chain` | Traverse source_ids to get the full reasoning chain |
| `honcho_extract_facts` | Structured deriver output (writes to extraction store) |

## Endpoints

- `POST /mcp` — MCP Streamable HTTP transport (JSON-RPC 2.0)
- `GET /extraction` — Pop extraction result (for ACP provider)
- `GET /health` — Health check

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `3100` | HTTP server port |
| `HONCHO_BASE_URL` | `http://localhost:8000` | Honcho API URL |
| `HONCHO_API_KEY` | `local` | API key (unused for self-hosted) |
| `HONCHO_WORKSPACE_ID` | `default` | Workspace ID |

## Development

```bash
npm install
npm run dev    # Run with tsx (hot reload)
npm run build  # Compile TypeScript
npm start      # Run compiled output
```

## Docker

The MCP server runs automatically inside the Honcho Docker container. The Dockerfile builds it in a separate stage and copies the output to `/app/mcp/`. The container command starts it alongside the API and deriver:

```
sh -c "alembic upgrade head && python -m src.deriver & node /app/mcp/dist/index.js & fastapi run --host 0.0.0.0 --port 8000 src/main.py"
```
