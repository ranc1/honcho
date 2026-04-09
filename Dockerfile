# ---------- Stage 1: Build TypeScript MCP ----------
FROM node:22-slim AS mcp-build
WORKDIR /mcp
COPY mcp/package.json mcp/package-lock.json* ./
RUN npm install
COPY mcp/src ./src
COPY mcp/tsconfig.json ./
RUN npx tsc

# ---------- Stage 2: Python app ----------
FROM python:3.13-slim-bookworm

# Install Node.js runtime (no build tools needed)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install Python dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-group dev

COPY uv.lock pyproject.toml /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-group dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/app
ENV UV_CACHE_DIR=/tmp/uv-cache

# Create non-root user
RUN addgroup --system app && adduser --system --group app && mkdir -p /tmp/uv-cache && chown -R app:app /app /tmp/uv-cache

# Copy Python app
COPY --chown=app:app src/ /app/src/
COPY --chown=app:app migrations/ /app/migrations/
COPY --chown=app:app scripts/ /app/scripts/
COPY --chown=app:app alembic.ini /app/alembic.ini
COPY --chown=app:app config.toml* /app/

# Copy built TS MCP from build stage
COPY --from=mcp-build --chown=app:app /mcp/dist /app/mcp/dist
COPY --from=mcp-build --chown=app:app /mcp/node_modules /app/mcp/node_modules

# Copy entrypoint
COPY --chown=app:app docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

USER app

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/openapi.json')" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
