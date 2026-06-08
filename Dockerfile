# Agent Accountant — single Cloud Run service: FastAPI serves the built React
# cockpit + the /api control plane, and spawns the Phoenix MCP server for the
# live "Ask the Accountant" introspection.

# ---- stage 1: build the React cockpit ----
FROM node:20-slim AS webbuild
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --no-fund --no-audit
COPY web/ ./
ENV VITE_API_BASE=""
RUN npm run build

# ---- stage 2: python + node runtime ----
FROM python:3.12-slim

# node (for the Phoenix MCP subprocess) + curl/ca-certs
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Pre-install the Phoenix MCP server globally so the agent can call the
# `phoenix-mcp` binary directly at runtime (no npx, no writable cache needed).
RUN npm install -g @arizeai/phoenix-mcp@4.0.13 && npm cache clean --force

# uv (fast Python dependency manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

# App source + the baked corpus snapshot (data/accountant.db). .dockerignore
# keeps node_modules / web build / caches out but lets the corpus through.
COPY . /app
# The freshly-built UI from stage 1
COPY --from=webbuild /web/dist /app/web/dist

# Install Python deps + the local package (frozen to uv.lock).
RUN uv sync --frozen --no-dev

# Runtime config: writable DB in /tmp (Cloud Run fs is read-only), call the
# MCP binary directly, Gemini via API key (set GOOGLE_API_KEY at deploy).
ENV ACCOUNTANT_DB=/tmp/accountant.db \
    PHOENIX_MCP_COMMAND=phoenix-mcp \
    GOOGLE_GENAI_USE_VERTEXAI=false \
    PORT=8080

EXPOSE 8080
RUN chmod +x /app/infra/entrypoint.sh
CMD ["/app/infra/entrypoint.sh"]
