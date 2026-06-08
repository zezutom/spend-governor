# Deploy

Agent Accountant runs as **one Cloud Run service**: FastAPI serves the built
React cockpit, the `/api` control plane, and spawns the Phoenix MCP server for
the live "Ask the Accountant" introspection.

## One-time

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT>
```

## Deploy

Put the secrets in `.env` (already used for local dev) or export them, then:

```bash
./infra/deploy.sh
```

Required env (sourced from `.env` if present):

| var | what |
|---|---|
| `PHOENIX_API_KEY_OBSERVED_WRITE` | Phoenix Cloud API key (reads traces via MCP) |
| `PHOENIX_COLLECTOR_ENDPOINT` | e.g. `https://app.phoenix.arize.com/s/<space>` |
| `GOOGLE_API_KEY` | Gemini API key |
| `PHOENIX_PROJECT_NAME` | optional, default `agent-accountant` |

Optional: `GCP_PROJECT`, `REGION` (default `us-central1`), `SERVICE` (default
`agent-accountant`).

The script builds via Cloud Build (`--source .`), so Docker isn't needed locally.
On success it prints the URL; smoke-test with `curl -fsS <URL>/health`.

## How it fits Cloud Run

- **Single image** (`Dockerfile`): stage 1 builds the UI (`VITE_API_BASE=""` →
  same-origin `/api`), stage 2 is a Python+Node runtime that pre-installs the
  `phoenix-mcp` binary globally and bakes the corpus snapshot (`data/accountant.db`).
- **Read-only filesystem**: `infra/entrypoint.sh` copies the corpus to
  `/tmp/accountant.db` (the one writable path) on each cold start, and
  `ACCOUNTANT_DB` points there. Every instance therefore boots from a clean demo.
- **MCP without npx**: the agent calls the globally-installed `phoenix-mcp`
  binary directly (`PHOENIX_MCP_COMMAND=phoenix-mcp`) — npx would need a writable
  cache.
- **Gemini via API key** (`GOOGLE_GENAI_USE_VERTEXAI=false` + `GOOGLE_API_KEY`).
- `--min-instances 1` keeps a warm instance during judging; `--timeout 3600`
  lets the SSE stream stay open (CPU is allocated while it is).

The image was verified locally end-to-end (UI, `/api`, live MCP `get-trace`,
policy writes to `/tmp`) before this was committed.
