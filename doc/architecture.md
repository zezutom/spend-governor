# Architecture

Spend Governor is a runtime FinOps **platform**, not an analytics
tool. It works in two planes: a **learning plane** that reads traces and
decides what to govern, and an **enforcement plane** — a thin **wrapper**
the observed agent's tool/LLM traffic flows through — that intervenes in
real time. It never edits prompts or touches source.

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
**two destinations at once** (see "Data flow" below). Its tool and
model calls also flow through the **wrapper** (see below) — in the
demo via in-process tool wrappers + ADK callbacks, the local stand-in
for the network gateway a production deployment would route traffic
through.

### The wrapper (enforcement plane)

`src/governor/wrapper/` — the inline layer that acts on execution in
real time. Policy-driven, with no access to the observed agent's prompt,
tool logic, or internals.

- `cache.py` — semantic cache. Serves a tool result only when the new
  query is embedding-equivalent to a prior one (cosine ≥ threshold);
  embeddings memoized by exact string. The equivalence check is the
  quality guardrail — genuinely different queries still execute.
- `wrapper.py` — wraps tool calls (semantic-cache interception, tagging
  the span `accountant.cache_hit` and pricing it $0), routes simple
  requests to a cheaper model (tagging `accountant.modification =
  model_swap`), and writes the per-span/per-trace `governor.*` cost
  schema. Policy-driven; no prompt/source access.
- `store.py` — operator-activated policies + an append-only intervention
  log (every action, with cost avoided) + policy activation timestamps.

The wrapper only acts on an **operator-activated policy**. Activation
is a runtime control the customer *can* grant (route traffic through the
gateway) — unlike editing prompts or production code, which they won't.

### The trace bus (Phoenix)

[Phoenix Cloud](https://phoenix.arize.com/) (Arize's hosted Phoenix
instance) is the durable, queryable store of record. Project name:
`agent-accountant`. Auth via `PHOENIX_API_KEY_OBSERVED_WRITE` in
`.env`. Queryable through the `arize-phoenix-client` SDK and the
`@arizeai/phoenix-mcp` MCP server.

Phoenix has no outbound webhooks or streaming, so it can't push new
spans to the Governor. That's why the observed agent fans out
directly (real-time path) and the Governor backfills from Phoenix
on first run (historical path).

### The Governor (learning plane)

Attaches cost to every span, aggregates to per-task-type unit
economics, detects economically irrational patterns, quantifies the
savings of a runtime policy, and — once a policy is active —
**verifies the savings from the traces**.

`src/governor/` is one loose module — `agent.py` (the ADK agent that
reads traces via Phoenix MCP and writes a report) — and six packages:

**`pricing/`** — the cost model (pure, no I/O):
- `cost.py` — per-span / per-trace cost computation
- `gemini.py` — Gemini Flash / Pro / Flash-Lite per-1M-token rates
- `tools.py` — per-call rates for the observed-agent tools

**`wrapper/`** — the enforcement plane (see "The wrapper" above):
`cache.py`, `store.py`, `wrapper.py`.

**`pipeline/`** — real-time ingest & serving:
- `ingest_server.py` — FastAPI on `:8765`; receives spans, enqueues them
- `db.py` — SQLite store (outbox, spans incl. `cache_hit`, recommendations, live state)
- `worker.py` — drains the outbox, costs each span (cached calls priced $0), refreshes state
- `backfill.py` — historical import from Phoenix for new accounts

**`analytics/`** — the learning brains:
- `detection.py` — statistical detectors over the trace set
- `savings.py` — dedupes detector output into one costed **issue** per task class (+ a model-routing issue); projects per-ticket % and monthly $
- `recommendations.py` / `reasoning.py` — turn issues into operator-facing policy recommendations (Gemini authors the rationale; it does **not** rewrite prompts)
- `verification.py` — trace-measured cost-per-ticket before vs. after a policy's activation time — the audit proof
- `analysis.py` + `agent_tools.py` — one-shot bulk pull/breakdown + the agent's tool functions

**`ui/`** — `dashboard.py`, the Streamlit UI; one command boots the whole stack.

**`cli/`** — secondary entry points: `main.py` (runs `agent.py`),
`inspect_traces.py`, `verify_cost.py`.

See [realtime-pipeline.md](./realtime-pipeline.md) for the runtime path and
[cost-model.md](./cost-model.md) for cost attribution.

## Data flow

```
┌──────────────────────────────────────────────────────────────────┐
│  OBSERVED AGENT  — tool & model calls flow through the WRAPPER     │
│  (cache redundant tools · route simple reqs to cheaper model)      │
│  emits each OTEL span — incl. accountant.cache_hit / model_swap —  │
│  to TWO exporters at once:                                         │
└───────────────┬──────────────────────────────┬───────────────────┘
                │ (1) Phoenix (system of record)│ (2) real-time
                ▼                                ▼
┌─────────────────────────────┐   ┌──────────────────────────────────┐
│  Phoenix Cloud              │   │  POST /ingest  (FastAPI :8765)    │
│  durable store · MCP · SDK  │   │  → outbox INSERT → 200            │
│  (filter governor.* tags) │   └───────────────┬──────────────────┘
└───────────────┬─────────────┘                   │ worker.py drains;
                │ backfill (new account)           │ cached calls priced $0
                ▼                                  ▼
        ┌───────────────────────────────────────────────────────┐
        │  SQLite: spans · live_state · policies · interventions │
        │  detection → savings (issues) → recommendations        │
        └───────────────────────────┬───────────────────────────┘
                       activate policy │   ▲ reads
                                       ▼   │
                          ┌────────────────────────────┐
                          │  dashboard.py (Streamlit)   │
                          │  hero: avoidable waste / $   │
                          │  saved · policy cards ·      │
                          │  trace-verified before/after │
                          └──────────────┬──────────────┘
                                         │ Activate policy → wrapper store
                                         ▼  (wrapper reads it, enforces live)
```

The loop: traces → detect & quantify → operator activates a policy →
the wrapper enforces it on live traffic → the optimized calls are
re-traced (tagged) → the Governor re-measures the before/after from
those traces. The savings are proven from the system of record, not
claimed.

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

(In production the inline wrapper is a network gateway on Cloud Run and
the store is a managed queue + BigQuery; here they're in-process + SQLite
so the whole demo runs from one command.)

## Cost tracking — Phoenix is the source of truth for LLM actuals

Phoenix natively computes the **actual cost** of every LLM call from
Gemini's token-count attributes and a model pricing table configured in
the Phoenix UI. We don't duplicate that math. Anywhere a dashboard or
report needs "what did this call actually cost," the answer comes from
Phoenix's native `cost` field — not from local computation. This makes
the savings claim independently verifiable in the customer's own
Phoenix UI, without trusting our dashboard.

Two important asymmetries:

- **Tool actuals are ours.** Phoenix's pricing table is for models; it
  doesn't price tool calls. The wrapper writes `accountant.cost.actual_usd`
  on tool spans from our local `TOOL_PRICES` table.
- **Counterfactuals are ours.** Phoenix has no concept of "what this
  would have cost without the policy." On modified spans the wrapper
  writes `accountant.cost.baseline_usd` + `accountant.cost.savings_usd`
  + `accountant.counterfactual.*` (the would-have-been parameters used
  to derive baseline). This is the part Phoenix can't help with — and
  the part that proves the savings claim.

The full schema is in [`instrumentation-schema.md`](./instrumentation-schema.md).
Phoenix's model pricing config and our tool pricing table together form
the single audit reference, documented in
[`phoenix-pricing-config.md`](./phoenix-pricing-config.md).

## Boundaries

- **The wrapper never edits prompts or source.** It acts only at the
  traffic boundary, on operator-activated policies. Integration is one
  hop (route traffic through the gateway), framework-agnostic.
- **We don't compete with Phoenix on cost.** Actual LLM cost comes from
  Phoenix's native pricing pipeline. We own only tool actual cost and
  the counterfactual / savings math — see the section above.
- **Quality is guarded.** A cached result is served only when the new
  query is semantically equivalent to a prior one; only low-risk task
  types are routed to the cheaper model. "Cost down, quality held" is
  the contract.
- **Savings are proven, not claimed.** Realized savings come from the
  wrapper's intervention log; verified savings are re-measured from the
  traces (before vs. after activation). Optimized calls are tagged in
  Phoenix (`accountant.cache_hit`, `accountant.modification == 'model_swap'`).
- **No real external calls in the observed agent.** Its tools return
  synthetic data — reproducible, no accidental side effects.
- **The SQLite cache is disposable** — rebuilt from the live stream plus
  the Phoenix backfill; gitignored, safe to delete.
- **One source of truth for the UI.** Every dashboard element reads a
  single `live_state` blob — see [realtime-pipeline.md](./realtime-pipeline.md).
