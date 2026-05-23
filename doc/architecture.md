# Architecture

This project has two agents and a trace bus between them.

## Components

### The observed agent

A small customer-support agent — Helpdesk Co-Pilot for the fictional
Stratus Forms SaaS. Built on Google ADK (`google-adk` Python
package), calls `gemini-2.5-flash` via Vertex AI, and uses seven
local tools (none reach the real internet — all return synthetic
data).

Lives in `src/observed/`. See [observed-agent.md](./observed-agent.md)
for the tools and the agent instruction.

The observed agent emits OpenTelemetry traces via
`arize-phoenix-otel`, so every LLM call and every tool call becomes
a span with token counts, latencies, and structured input/output in
OpenInference's semantic conventions.

### The trace bus

[Phoenix Cloud](https://phoenix.arize.com/) (Arize's hosted Phoenix
instance) collects the traces. Project name: `agent-accountant`.
Authentication via `PHOENIX_API_KEY_OBSERVED_WRITE` in `.env`. The
project is queryable through the `arize-phoenix-client` SDK and
through Phoenix's MCP server.

### The Accountant

Reads traces from Phoenix, attaches cost (LLM tokens × model price +
tool calls × per-call price), aggregates to per-trace and per-task-
type unit economics, detects anomalies, and recommends optimizations.

Lives in `src/accountant/`. Today this consists of:

- `cost.py` — pure cost-computation functions, no I/O
- `pricing/gemini.py` — Gemini 2.5 Flash and Pro per-1M-token rates
- `pricing/tools.py` — per-call rates for the seven observed-agent tools
- `verify_cost.py` — sanity check that the math is right
- `inspect_traces.py` — pulls spans from Phoenix, prints per-trace and
  per-task-type breakdowns

The Accountant agent itself — the ADK loop that reads traces, calls
Gemini for recommendations, and writes config changes back — is in
development.

See [cost-model.md](./cost-model.md) for how cost attribution works.

## Data flow

```
┌─────────────────────────────────────────────────────────┐
│  src/observed/  (Helpdesk Co-Pilot)                     │
│  ADK agent + Gemini 2.5 Flash + 7 synthetic tools       │
└────────────────────────────┬────────────────────────────┘
                             │ OTEL spans
                             │ (arize-phoenix-otel)
                             ▼
┌─────────────────────────────────────────────────────────┐
│  Phoenix Cloud — project: agent-accountant              │
│  Stores spans, exposes them via SDK + MCP               │
└────────────────────────────┬────────────────────────────┘
                             │ arize-phoenix-client
                             │ (or @arizeai/phoenix-mcp)
                             ▼
┌─────────────────────────────────────────────────────────┐
│  src/accountant/  (cost analysis + aggregation)         │
│  Reads spans, computes per-trace cost, aggregates by    │
│  task type and customer                                 │
└─────────────────────────────────────────────────────────┘
```

## Stack as built

- **Agent framework:** [Google ADK](https://github.com/google/adk-python) (Python)
- **LLM:** Gemini 2.5 Flash via Vertex AI (Pro is available for the Accountant's reasoning step)
- **Observability:** [Phoenix Cloud](https://phoenix.arize.com/) (hosted Arize Phoenix)
- **Trace instrumentation:** OpenTelemetry via `arize-phoenix-otel` + `openinference-instrumentation-google-adk`
- **Package manager:** [uv](https://docs.astral.sh/uv/)
- **License:** MIT

(The README's "Built with" section reflects the planned production
deployment — Cloud Run, Firestore, Agent Builder. The stack above
is what's actually wired up today.)

## Boundaries

- **No real external calls in the observed agent.** The seven tools
  all return synthetic data. This keeps the project reproducible
  and avoids accidental side effects.
- **The Accountant reads but does not modify traces.** Phoenix is
  append-only from the Accountant's perspective.
- **Cost computation is deterministic and stateless.** `cost.py`
  has no I/O, no caching, no hidden state — given the same usage
  metadata and prices, it returns the same breakdown every time.
