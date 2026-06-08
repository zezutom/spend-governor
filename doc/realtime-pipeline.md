# Real-time pipeline

How spans get from the observed agent into the live dashboard, and why
the pieces are shaped the way they are.

## One command boots everything

```bash
./scripts/start-cockpit.sh
```

This starts the control-plane API (`:8800`), the ingest server (`:8765`),
and the React cockpit (Vite, `:5173`; open this). On first run it:

1. Probes `127.0.0.1:8765`; if nothing is listening, spawns the ingest
   server (`uvicorn accountant.pipeline.ingest_server:app`) as a background
   subprocess.
2. Checks the SQLite cache. If empty (a new account), POSTs
   `/backfill/start` to import history from Phoenix.
3. Renders from the `live_state` blob, refreshing every 500ms.

No separate processes to start, no manual buttons in the common path.

## Fan-out: two exporters on the observed agent

`src/observed/telemetry.py` registers Phoenix's OTEL exporter as usual,
then adds a second `BatchSpanProcessor` wired to
`AccountantHTTPExporter` (`src/observed/accountant_exporter.py`). The
same spans go to Phoenix (durable store) and to the Accountant
(`POST /ingest`, real-time) simultaneously.

The second exporter activates only when `ACCOUNTANT_INGEST_URL` is set.
When it's unset, the agent logs a one-line notice and emits to Phoenix
only — so a missing env var is visible, not a silent no-op.

> **Gotcha:** Phoenix's `TracerProvider.add_span_processor` defaults to
> `replace_default_processor=True`, which shuts down Phoenix's own
> exporter when you add a second one. We pass
> `replace_default_processor=False` so both Phoenix and the Accountant
> receive every span — Phoenix is the system of record and the audit
> proof, so it must keep receiving traffic.

Export is best-effort: if the Accountant is down, the export fails, OTel
logs a warning, and Phoenix still receives everything. (A production
emitter would need a durable local queue; in this setup both processes
run side by side.)

## Outbox pattern at the ingest boundary

`POST /ingest` does the minimum and returns immediately:

```
receive span batch → INSERT one row into span_outbox → COMMIT → 200
```

No cost computation, no detection on the request path. The durability
boundary is that single transactional INSERT. A separate async worker
(`worker.py`, an asyncio task in the same process) drains the outbox:

```
claim pending rows → parse spans → compute cost (cost.py)
  → UPSERT into spans table → recompute detection + recommendations
  → write live_state
```

Why an outbox rather than processing inline:

- **Bounded receiver latency.** The HTTP handler can't be slowed by
  cost math or aggregation.
- **Durability.** A crash mid-processing leaves the outbox row marked
  `pending`; it's retried, not lost.
- **Idempotency.** `span_id` is the primary key in the `spans` table;
  re-ingesting a span is a no-op (`ON CONFLICT DO NOTHING`).

In production the outbox would be Kafka or a managed queue. SQLite (WAL
mode) is the MVP stand-in, which lets the FastAPI receiver and the
worker write concurrently without blocking each other.

## SQLite schema (`db.py`)

| Table | Purpose |
|-------|---------|
| `span_outbox` | the queue — incoming span batches awaiting processing |
| `spans` | every ingested span, with cost attached (incl. `cache_hit`, priced $0) |
| `recommendations` | one costed issue per task class, keyed by issue signature |
| `accountant_policies` | operator-activated rules the wrapper enforces (with activation time) |
| `accountant_interventions` | append-only log of every wrapper action + cost avoided |
| `state_meta` | key/value store; holds the `live_state` blob the dashboard reads |

WAL mode is enabled so the receiver, worker, backfill, and the wrapper
(running in the observed agent's process) can all touch the store
without blocking each other.

## Onboarding backfill (`backfill.py`)

A new account has an empty cache, so the live stream alone would show
nothing until the next ticket. The backfill imports history from
Phoenix on first run:

- Triggered by the dashboard (`POST /backfill/start`) only when the
  spans table is empty. Existing accounts skip it.
- Walks Phoenix **newest → oldest in 10-minute chunks** (Phoenix Cloud
  disconnects on single responses larger than ~10k spans, so the window
  is chunked).
- Inserts **per span**, updating `live_state` after each, so the
  dashboard counters tick continuously rather than jumping per chunk.
- **Exits as soon as the imported trace count reaches the project's
  estimated total** (queried up front from Phoenix). Failing that, it
  stops after a long run of consecutive empty chunks (end of history)
  or a hard 90-day floor.

Progress is reported through `live_state.ingest` (humanized: "Found
activity from yesterday", not chunk indices or UTC slice boundaries).

## Single source of truth: `live_state`

Every dashboard element — the onboarding banner counters, the header,
the by-class table, the anomaly cards — reads **one** blob:
`state_meta.live_state`. Its shape:

```json
{
  "ingest":   { "status", "message", "progress", "estimated_total_traces", ... },
  "summary":  { "total_traces", "total_spans", "total_cost_usd", ... },
  "by_task_class": { "<class>": { "n", "avg_cost_usd", ... } },
  "anomalies": [ { "type", "task_class", ... } ]
}
```

It is computed from one in-memory trace store and written atomically.
There is deliberately **not** a second state key updated on a different
cadence — an earlier design had per-span counters and per-chunk
aggregates in separate keys, and they diverged mid-chunk (banner said
707 traces, header said 603). One store, one blob, one read.

Both writers use the same key:
- `backfill.py` writes `live_state` per span during onboarding.
- `worker.py` writes `live_state` after each live batch.

## Detect → quantify → activate → enforce → verify

This is the product loop. Each step:

**Detect** (`detection.py`) — two statistical detectors over the traces:
- `class_cost_uplift` — a task class averaging ≥ 2.0× the `password_reset` baseline.
- `repeated_tool` — a tool fired ≥ 3 times within a trace, in ≥ 10% of that class.

**Quantify** (`savings.py`) — dedupe the raw detector output into **one
costed issue per task class** (a refund's cost-uplift + its repeated
web_search collapse into a single issue), plus a model-routing issue for
simple classes. Each issue carries projected per-ticket % and a monthly
$ projection from the observed traffic window. `recommendations.py` /
`reasoning.py` turn issues into operator-facing cards (Gemini authors the
rationale; `📋 pattern` upgrades to `🤖 reasoned`).

**Activate** — the operator clicks **Activate policy** on a card. That
writes a `accountant_policies` row (e.g. `cache_tool:web_search`,
`route_model:simple`) with the activation timestamp. No prompt or source
change — a runtime control the customer can grant.

**Enforce** (`src/accountant/wrapper/`) — the wrapper runs inline in the
observed agent's call path. On an active policy it:
- **Caches tools** — before executing a governed tool (e.g. `web_search`),
  it checks the semantic cache (`cache.py`). On a hit (query embedding
  cosine ≥ threshold against a prior call) it returns the cached result,
  the real paid call never fires, and it tags the span
  `accountant.cache_hit` so the cost model prices it **$0**. Different
  queries miss and execute normally — the quality guardrail.
- **Routes models** — a `before_model_callback` downgrades simple
  requests to a cheaper model and tags the span
  `accountant.modification = model_swap`.
- Records each action in `accountant_interventions` with the cost avoided.

**Verify** (`verification.py`) — the audit proof. For the task types a
policy affects, it compares average cost-per-ticket **before** the
policy's activation time vs **after**, straight from the traces. Because
cached calls are priced $0 and routed calls carry the cheaper model, the
"after" cost genuinely drops. The dashboard shows this beside the
wrapper's own realized-savings log; when they agree, the number is
trustworthy. Optimized calls are filterable in Phoenix by the
`accountant.*` tags.

## Accuracy

The headline trace count equals what Phoenix actually holds — all
trace IDs are surfaced. Traces that are partial (fewer than two tool
spans) or have no classifier output are **not** dropped; they appear
under an `unknown` task class so the totals reconcile. Nothing is
silently filtered out of the count.

## Ports

- `8765` — Accountant ingest server (FastAPI). No UI; `/ingest`,
  `/backfill/start`, `/health`.
- `8800` — control-plane API (FastAPI): cockpit state + SSE + `/api/ask`.
- `5173` — React cockpit (Vite). The UI.
