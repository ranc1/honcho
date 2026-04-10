<!-- markdownlint-disable MD033 -->
<div align="center">
  <a href="https://github.com/plastic-labs/honcho" target="_blank">
    <img src="assets/honcho.svg" alt="Honcho" width="400">
  </a>
</div>
<!-- markdownlint-enable MD033 -->

---

Fork of [Honcho](https://github.com/plastic-labs/honcho) (v3.0.6) that adds an ACP (Agent Client Protocol) provider. Instead of calling LLM APIs directly, Honcho routes all LLM calls through an external ACP-compatible gateway via HTTP. All existing reasoning modules (deriver, dreamer, dialectic, summarizer) work without modification.

This fork also fixes upstream bugs in the Conclusion API (`level` and `source_ids` fields) and configures embeddings for a local open-source model (`qwen3-embedding:0.6b` via Ollama, 1024 dimensions).

## ACP Provider

### Configuration

| Env Var | Type | Default | Description |
|---|---|---|---|
| `LLM_ACP_GATEWAY_URL` | `str` | `None` | ACP gateway URL. Enables the ACP provider when set. |
| `LLM_ACP_TIMEOUT_MS` | `int` | `300000` | Request timeout in ms (1–600000). |

Set any module's provider to `acp` to route its LLM calls through the gateway:

```env
DERIVER_PROVIDER=acp
DREAM_PROVIDER=acp
SUMMARY_PROVIDER=acp
DIALECTIC_LEVELS__minimal__PROVIDER=acp
DIALECTIC_LEVELS__low__PROVIDER=acp
DIALECTIC_LEVELS__medium__PROVIDER=acp
DIALECTIC_LEVELS__high__PROVIDER=acp
DIALECTIC_LEVELS__max__PROVIDER=acp
```

When using the ACP provider, you don't need LLM API keys — the gateway handles model routing. Dummy values may be needed for providers that Honcho validates at startup:

```env
LLM_OPENAI_API_KEY=unused
LLM_ANTHROPIC_API_KEY=unused
```

### Embedding Configuration

This fork defaults to `qwen3-embedding:0.6b` (1024 dimensions) via Ollama's OpenAI-compatible endpoint:

```env
LLM_OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1
LLM_OPENAI_COMPATIBLE_API_KEY=unused
LLM_EMBEDDING_PROVIDER=openrouter
VECTOR_STORE_DIMENSIONS=1024
```

### ACP Gateway Contract

The ACP gateway must expose:

```
POST {LLM_ACP_GATEWAY_URL}/api/v1/acp/prompt

Request:  { "module": "deriver"|"dreamer"|"dialectic"|"summarizer",
            "prompt": "...",
            "systemPrompt": "..." }

Response: { "text": "..." }
```

The gateway registers per-module MCP server URLs when creating ACP sessions for Honcho modules (e.g., `http://{honcho_host}:8000/mcp/dreamer` for the dreamer session).

## MCP Architecture

This fork exposes two MCP servers:

### Internal Tool MCP (port 8000, per-module endpoints)

Serves Honcho's internal reasoning modules (dreamer, dialectic, deriver). Dispatches tools via `_TOOL_HANDLERS` using a `tool_executor` closure created during HTTP context registration.

- **Endpoints**: `POST /mcp/{dreamer,dialectic,deriver}` (JSON-RPC)
- **Context**: `POST /mcp/{module}/context` (register), `DELETE /mcp/{module}/context` (deregister)
- **Tools (16)**: 15 internal tools auto-generated from `agent_tools.TOOLS` dict + `honcho_extract_facts`
- **Tool names**: `create_observations`, `delete_observations`, `search_memory`, `search_messages`, `grep_messages`, `get_messages_by_date_range`, `search_messages_temporal`, `get_observation_context`, `get_recent_observations`, `get_most_derived_observations`, `get_peer_card`, `update_peer_card`, `finish_consolidation`, `extract_preferences`, `get_reasoning_chain`, `honcho_extract_facts`

### Client-Facing TS MCP (port 8001)

Serves external consumers (CLI engines, integrations). Reuses the upstream TypeScript MCP code with only the entry point changed from Cloudflare Workers to a self-hosted Node.js HTTP server.

- **Endpoint**: `POST /` (Streamable HTTP transport with session management)
- **Tools (30)**: All upstream tools including `query_conclusions`, `create_conclusions`, `chat`, `get_peer_card`, `get_session_messages`, `search`, etc.
- **Config**: `MCP_WORKSPACE_ID`, `HONCHO_BASE_URL`, `HONCHO_API_KEY` env vars

## Local Development

Below is a guide on setting up a local environment for running the Honcho
Server.

> This guide was made using a M3 Macbook Pro. For any compatibility issues
> on different platforms, please raise an Issue.

### Prerequisites and Dependencies

Honcho is developed using [python](https://www.python.org/) and [uv](https://docs.astral.sh/uv/).

The minimum python version is `3.10`
The minimum uv version is `0.5.0`

### Setup

1. **Clone the repository**

```bash
git clone https://github.com/ranc1/honcho.git
cd honcho
git checkout acp-provider
```

2. **Install dependencies**

```bash
uv sync
source .venv/bin/activate
```

3. **Set up a database**

Honcho utilizes [Postgres](https://www.postgresql.org/) with pgvector.
A `docker-compose` template is available:

```bash
cp docker-compose.yml.example docker-compose.yml
docker compose up -d database
```

4. **Edit the environment variables**

Copy the template and fill in the required values:

```env
DB_CONNECTION_URI=              # PostgreSQL connection URI (postgresql+psycopg prefix)
LLM_ACP_GATEWAY_URL=           # ACP gateway URL (e.g. http://localhost:3456)
AUTH_USE_AUTH=false              # Disable auth for local deployment
```

See `.env.template` for the full list of configuration options.

5. **Run database migrations**

```bash
uv run alembic upgrade head
```

6. **Launch Honcho**

Start the API server:

```bash
uv run fastapi dev src/main.py
```

Start a background worker (deriver) in a separate terminal:

```bash
uv run python -m src.deriver
```

The deriver generates representations, summaries, peer cards, and manages dreaming tasks.

### Pre-commit Hooks

Honcho uses pre-commit hooks to ensure code quality and consistency.

```bash
uv add --dev pre-commit
uv run pre-commit install \
    --hook-type pre-commit \
    --hook-type commit-msg \
    --hook-type pre-push
```

The hooks include: Python linting/formatting (ruff), TypeScript linting (biome), type checking (basedpyright), security scanning (bandit), markdown linting, license header checks, and conventional commit validation.

Run manually:

```bash
uv run pre-commit run --all-files
```

### Docker

A `docker-compose` template is included. Copy the template and update environment variables:

```bash
cp .env.template .env
cp docker-compose.yml.example docker-compose.yml
docker compose up
```

## Configuration

Honcho uses a flexible configuration system that supports both TOML files and environment variables.

### Using config.toml

```bash
cp config.toml.example config.toml
```

The TOML file is organized into sections:

- `[app]` - Application-level settings (log level, session limits, embedding settings, namespace)
- `[db]` - Database connection and pool settings
- `[auth]` - Authentication configuration
- `[cache]` - Redis cache configuration
- `[llm]` - LLM provider API keys and general settings
- `[deriver]` - Background worker settings and representation configuration
- `[peer_card]` - Peer card generation settings
- `[dialectic]` - Dialectic API configuration with per-level reasoning settings
- `[summary]` - Session summarization settings
- `[dream]` - Dream processing configuration
- `[webhook]` - Webhook configuration
- `[metrics]` - Prometheus pull-based metrics
- `[telemetry]` - CloudEvents telemetry for analytics
- `[vector_store]` - Vector store configuration (pgvector, turbopuffer, or lancedb)
- `[sentry]` - Error tracking and monitoring settings

### Using Environment Variables

All configuration values can be overridden using environment variables:

- `{SECTION}_{KEY}` for nested settings
- Just `{KEY}` for app-level settings

Examples:

- `DB_CONNECTION_URI` - Database connection string
- `AUTH_JWT_SECRET` - JWT secret key
- `DIALECTIC_LEVELS__low__MODEL` - Model for low reasoning level
- `DERIVER_PROVIDER` - Provider for background deriver
- `SUMMARY_PROVIDER` - Summary generation provider

### Configuration Priority

1. **Environment variables** - Always take precedence
2. **.env file** - Loaded for local development
3. **config.toml** - Base configuration
4. **Default values** - Built-in defaults

## Architecture

The functionality of Honcho can be split into two different services: Storage
and Reasoning.

### Peer Paradigm

Honcho uses an entity-centric model where both users and agents are represented as "[peers](https://blog.plasticlabs.ai/blog/Beyond-the-User-Assistant-Paradigm;-Introducing-Peers)". This unified approach enables:

- Multi-participant sessions with mixed human and AI agents
- Configurable observation settings (which peers observe which others)
- Flexible identity management for all participants
- Support for complex multi-agent interactions

### Storage

Honcho contains several primitives for storing application and peer data:

```
Workspaces
├── Peers ←──────────────────┐
│   ├── Sessions             │
│   └── Collections          │
│       └── Documents        │
│                            │
└── Sessions ←───────────────┤ (many-to-many)
    ├── Peers ───────────────┘
    └── Messages (session-level)
```

- **Workspaces** — Top-level isolation for different apps or tenants
- **Peers** — Any participant (human or AI agent)
- **Sessions** — A set of interactions between peers (thread/conversation)
- **Messages** — Atomic data units labeled by source peer
- **Collections** — Named groups of vector-embedded documents per peer
- **Documents** — Vector-embedded data stored in collections

### Reasoning

As messages and sessions are created, Honcho asynchronously reasons about peer psychology:

1. Messages are created via the API
2. Derivation tasks are enqueued for background processing:
   - `representation` — update representations of peers
   - `summary` — create summaries of sessions
3. Session-based queue processing ensures proper ordering
4. Results are stored internally

The ACP provider intercepts at the provider dispatch level (`honcho_llm_call_inner`), so all reasoning modules work as documented upstream — the only difference is where the LLM calls are routed.

### Retrieving Data & Insights

- **Context** — Returns messages, conclusions, and summaries from a session up to a token limit
- **Search** — Hybrid search at workspace, session, or peer level with advanced filters
- **Chat API** — Natural language endpoint (`/peers/{peer_id}/chat`) for querying insights about a peer
- **Representations** — Low-latency static documents with insights about a peer in a session context

## Contributing

We welcome contributions! Please read the [Contributing Guide](./CONTRIBUTING.md) for details.

## License

Honcho is licensed under the AGPL-3.0 License. See [LICENSE](./LICENSE).
