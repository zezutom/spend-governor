# Architecture

Two agents, with the observed agent's traces flowing to the Accountant
by two paths: a real-time event stream and a historical store.

## Components

### The observed agent

A small customer-support agent — Helpdesk Co-Pilot for the fictional
Stratus Forms SaaS. Built on Google ADK (`google-adk` Python
package), calls `gemini-2.5-flash` via Vertex AI, and uses seven
local tools (none reach the real internet — all return synthetic
data).

Lives in `src/observed/`. See [observed-agent.md](./observed-agent.md)
for the tools and the agent instruction.

Every LLM call and every tool call becomes an OpenTelemetry span with
token counts, latencies, and structured input/output in
OpenInference's semantic conventions. The agent emits each span to
**two destinations at once** (see "Data flow" below).

### The trace bus (Phoenix)

[Phoenix Cloud](https://phoenix.arize.com/) (Arize's hosted Phoenix
instance) is the durable, queryable store of record. Project name:
`agent-accountant`. Auth via `PHOENIX_API_KEY_OBSERVED_WRITE` in
`.env`. Queryable through the `arize-phoenix-client` SDK and the
`@arizeai/phoenix-mcp` MCP server.

Phoenix has no outbound webhooks or streaming, so it can't push new
spans to the Accountant. That's why the observed agent fans out
directly (real-time path) and the Accountant backfills from Phoenix
on first run (historical path).

### The Accountant

Attaches cost to every span, aggregates to per-trace and per-task-type
unit economics, detects anomalies, and surfaces remediation
recommendations on a live dashboard.

Lives in `src/accountant/`. Two layers:

**Cost core (pure, no I/O):**
- `cost.py` — per-span / per-trace cost computation
- `pricing/gemini.py` — Gemini 2.5 Flash & Pro per-1M-token rates
- `pricing/tools.py` — per-call rates for the seven observed-agent tools
- `verify_cost.py` — regression check for the math

**Real-time pipeline (see [realtime-pipeline.md](./realtime-pipeline.md)):**
- `ingest_server.py` — FastAPI on `:8765`; receives spans, enqueues them
- `db.py` — SQLite store (outbox queue, spans, recommendations, live state)
- `worker.py` — drains the outbox, costs each span, refreshes state
- `detection.py` — statistical anomaly detectors
- `recommendations.py` — templated remediation text per anomaly
- `backfill.py` — historical import from Phoenix for new accounts
- `dashboard.py` — Streamlit UI; single command that boots the whole stack

**CLI / batch tools (secondary):**
- `inspect_traces.py` + `analysis.py` — one-shot bulk pull from Phoenix,
  prints per-trace and by-class breakdowns
- `agent.py` + `main.py` — an ADK agent that reads traces via the Phoenix
  MCP server and writes a JSON report (the conversational analysis path)

The Gemini-driven reasoning layer that turns templated recommendations
into individually-authored ones, and the approval-to-config-write loop,
are in development.

See [cost-model.md](./cost-model.md) for how cost attribution works.

## Data flow

```
┌──────────────────────────────────────────────────────────────────┐
│  src/observed/  (Helpdesk Co-Pilot)                               │
│  ADK agent + Gemini 2.5 Flash + 7 synthetic tools                 │
│  emits each OTEL span to TWO exporters:                           │
└───────────────┬──────────────────────────────┬───────────────────┘
                │ (1) historical                │ (2) real-time
                │ arize-phoenix-otel            │ AccountantHTTPExporter
                ▼                                ▼
┌─────────────────────────────┐   ┌──────────────────────────────────┐
│  Phoenix Cloud              │   │  POST /ingest  (FastAPI :8765)    │
│  project: agent-accountant  │   │  → transactional outbox INSERT    │
│  durable store, MCP, SDK    │   │  → 200 immediately                │
└───────────────┬─────────────┘   └───────────────┬──────────────────┘
                │ backfill.py (new-account             │ worker.py
                │  onboarding: 10-min chunks)          │  drains outbox
                ▼                                       ▼
        ┌───────────────────────────────────────────────────────┐
        │  SQLite (db.py): spans + recommendations + live_state │
        │  cost attached per span via cost.py                   │
        │  detection.py + recommendations.py refresh live_state │
        └───────────────────────────┬───────────────────────────┘
                                     │ one read
                                     ▼
                          ┌────────────────────────┐
                          │  dashboard.py          │
                          │  (Streamlit, :8501)    │
                          │  live counters, by-    │
                          │  class table, rec cards│
                          └────────────────────────┘
```

Both ingest paths converge on the same SQLite `spans` table. The
real-time path keeps the dashboard current as the observed agent runs;
the backfill path populates history the first time a new account
connects (empty cache).

## Stack as built

- **Agent framework:** [Google ADK](https://github.com/google/adk-python) (Python)
- **LLM:** Gemini 2.5 Flash via Vertex AI (Pro available for the reasoning step)
- **Observability:** [Phoenix Cloud](https://phoenix.arize.com/) (hosted Arize Phoenix)
- **Trace instrumentation:** OpenTelemetry via `arize-phoenix-otel` + `openinference-instrumentation-google-adk`
- **Ingest service:** FastAPI + uvicorn
- **Local store:** SQLite (WAL mode)
- **Dashboard:** Streamlit
- **Package manager:** [uv](https://docs.astral.sh/uv/)
- **License:** MIT

(The README's "Built with" section reflects the planned production
deployment — Cloud Run, Firestore, Agent Builder. The stack above is
what's wired up today. The SQLite store stands in for what would be
BigQuery/Firestore in production.)

## Boundaries

- **No real external calls in the observed agent.** The seven tools
  all return synthetic data — reproducible, no accidental side effects.
- **The Accountant reads Phoenix, never writes to it.** Phoenix is
  append-only from the Accountant's perspective.
- **Cost computation is deterministic and stateless.** `cost.py` has
  no I/O, no caching — same usage metadata and prices in, same
  breakdown out.
- **The SQLite cache is disposable.** It's rebuilt from the real-time
  stream plus the Phoenix backfill; it is gitignored and safe to delete.
- **One source of truth for the UI.** Every dashboard element reads a
  single `live_state` blob, computed from one in-memory trace store —
  see [realtime-pipeline.md](./realtime-pipeline.md).
