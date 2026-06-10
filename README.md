# Spend Governor

**AI Runtime FinOps — a FinOps platform that governs AI spend at runtime.
It stops wasteful AI spend as it happens, and proves the savings from
your own traces.**

A submission to the [Google Cloud Rapid Agent Hackathon](https://rapid-agent.devpost.com/).

**Required stack — all three imported and called at runtime:**
**Gemini** · **Google Cloud Agent Builder** — [Google ADK](https://github.com/google/adk-python), the code-owned agent runtime the Arize track mandates (the visual Agent Builder is explicitly not supported for tracing) · the **Arize Phoenix MCP server**. Details in [Built with](#built-with).

---

## Live demo

**▶ [agent-accountant-835758104453.us-central1.run.app](https://agent-accountant-835758104453.us-central1.run.app)** — the cockpit, running on Google Cloud Run.

Open it and watch the Governor govern a **fleet of four AI agents** on a
time-compressed clock (disclosed as *"last 12h · time-compressed"*):

1. It **auto-applies** the safe fixes — semantic-cache a redundant search loop,
   cap a runaway tool loop — and **$/message** steps down on the value spine.
2. It **escalates** the answer-affecting ones to you. Click **arm it**, or open
   the **debugger** to replay the change across real conversations and see the
   held/degraded split (a real neutral-judge eval panel) *before* you commit.
3. Click any agent → **🔍 ask the governor**: the real ADK agent calls the
   **Phoenix MCP server at runtime** to pull a real trace and explain the waste,
   grounded in the spans — with trace/span ids that link straight into Phoenix.

Everything is real — real ADK agents, real OTEL traces, real eval verdicts, real
measured savings. The only scripted thing is *when* each problem surfaces, and
the clock says so.

---

## What this does

Production AI agents quietly burn money on economically irrational
execution: the same external searches repeated on every ticket, an
over-powered model answering trivial requests, runaway tool loops. The
spend grows; nobody can point at *where* or stop it without an
engineering project.

Spend Governor is **not** an analytics dashboard. It's a runtime
FinOps **platform** whose enforcement plane is a thin **wrapper** in
front of your LLM/tool calls. It learns where the waste is from your
traces, then intervenes in real time at that wrapper your agents'
traffic flows through — capping redundant calls and serving a
semantically-equivalent cache, routing simple requests to a cheaper
model, preventing wasteful loops. **No prompt edits. No source access.
No engineering sprint.** You route traffic through it; it governs spend
at the boundary, and re-measures from traces to prove the cost fell
while quality held.

A worked example from the demo: a customer-support agent runs **three
redundant web searches on every refund ticket** — refunds cost 6× the
baseline. You click **Activate caching**. The wrapper starts serving
those searches from a semantic cache (only when the new query is
provably equivalent to a prior one), so the real, paid calls stop
firing. Within one batch of new refunds, the dashboard shows — measured
from the traces, not estimated — refund cost dropping from `$0.023` to
`$0.006` per ticket. Nothing in the agent's prompt or code changed.

---

## Why this, not analytics

The value is **autonomous runtime control**, not visibility. Observability
tells you what already happened and leaves the fix to your engineers.
The wrapper *acts*:

- **Framework-agnostic** — works across heterogeneous AI stacks; it sits
  at the traffic boundary, not inside your agents.
- **No trust/security barrier** — it never reads your prompts or repos,
  never modifies production AI behavior in code.
- **Provable financial impact** — every saving is measured from your own
  traces, before vs. after, and every intervention is tagged in the
  trace record.
- **Immediate** — no engineering effort to capture value; flip a policy
  and the spend drops on the next request.

---

## Architecture

Two planes. The **learning plane** reads traces and decides *what* to
govern. The **enforcement plane** is the inline gateway that *acts*.

```
            ┌───────────────────────────────────────────────┐
            │  OBSERVED AGENT  (your AI pipeline)            │
            │  resolves tickets · calls Gemini + tools       │
            └───────┬───────────────────────────────┬────────┘
       OTEL spans   │                                │  tool / LLM calls
       (to Phoenix) │                                │  route through ↓
                    ▼                                ▼
   ┌────────────────────────────┐   ┌────────────────────────────────────┐
   │  LEARNING PLANE            │   │  ENFORCEMENT PLANE (the wrapper)    │
   │  Arize/Phoenix traces →    │   │  inline gateway — intervenes live:  │
   │  attach cost → detect      │──▶│  • semantic-cache redundant tools   │
   │  wasteful patterns →       │   │  • route simple reqs → cheaper model│
   │  quantify $ → derive a     │   │  • cap loops / tool invocations     │
   │  policy                    │   │  (operator activates a policy)      │
   └────────────────────────────┘   └────────────────┬───────────────────┘
                    ▲                                 │ optimized calls,
                    │  re-measure from traces         │ tagged in traces
                    └─────────────────────────────────┘  (proof)
```

Phoenix is the system of record and the learning signal; it can't
intercept calls (it's post-hoc), so enforcement happens at the inline
gateway. Integration is one boundary — route traffic through the
gateway — not per-agent changes.

**The Arize/Phoenix MCP server is load-bearing two ways.** The learning
pipeline reads traces in bulk via the Phoenix SDK/GraphQL to attach cost and
detect waste. And the Governor — a Google ADK agent with the Phoenix MCP
server registered as a tool — **introspects its own operational data at
runtime**: in the *Ask the Governor* panel it calls `get-trace` /
`get-span-annotations` live to pull a real trace and ground its answer in the
spans. That agentic MCP loop (plan → call MCP → explain) is visible on screen,
not buried in code.

See [`doc/architecture.md`](./doc/architecture.md) for the full design and
[`doc/realtime-pipeline.md`](./doc/realtime-pipeline.md) for the runtime path.

---

## Built with

- **Google Cloud Agent Builder — [Google ADK](https://github.com/google/adk-python)** (Agent Development Kit): the Governor and the observed fleet are ADK agents. This is the **code-owned agent runtime the Arize track requires**:
  > "The Arize track requires a code-owned agent runtime — Gemini CLI, Gemini Enterprise Agent Platform SDK, Google ADK, Agent Runtime, or Cloud Run. The visual Agent Builder alone is not supported for tracing integration. You must be able to instrument your code directly."

  We build on ADK and deploy on Cloud Run for exactly that reason.
- [**Gemini**](https://deepmind.google/technologies/gemini/) — `gemini-2.5-flash` (live MCP chat), `gemini-2.5-pro` (neutral eval judge), Gemini embeddings (semantic cache)
- [**Arize / Phoenix**](https://phoenix.arize.com/) — OTEL traces via **OpenInference** auto-instrumentation for ADK, **introspected at runtime through the Phoenix MCP server**:
  > "Instrument your agent with OpenInference. Auto-instrumentors exist for Google ADK, Agent Platform, Google GenAI, LangChain, LlamaIndex and many other frameworks."
- FastAPI + SQLite — the gateway, control-plane API, and store
- React (Vite) — the operator cockpit (SSE-driven, live)
- Deployed on **Google Cloud Run** · [uv](https://docs.astral.sh/uv/) · MIT licensed

---

## Repository layout

```
.
├── src/
│   ├── governor/      Learning plane (cost model, detection, savings,
│   │                    verification, ingest, dashboard) + wrapper/ —
│   │                    the enforcement plane (semantic cache, tool
│   │                    interception, model routing, policy store)
│   │                    Also governor/agent.py — the ADK agent with the
│   │                    Phoenix MCP toolset (the "Ask the Governor" panel)
│   └── observed/        The observed agent fleet (the governed targets)
├── web/                 React (Vite) operator cockpit (the dashboard)
├── infra/               Cloud Run deploy (deploy.sh / teardown.sh); Dockerfile at root
├── examples/            Where the Governor agent writes its report (governor-report.json)
├── doc/
│   ├── architecture.md      Two-plane design overview
│   ├── realtime-pipeline.md Ingest, wrapper, policies, verification
│   ├── observed-agent.md    The Helpdesk Co-Pilot: tools and instruction
│   ├── cost-model.md        How per-trace / per-task-type cost is computed
│   └── development.md       Setup, environment, and common commands
├── LICENSE
├── CLAUDE.md             Guidance for Claude Code in this repo
└── README.md
```

---

## Running it locally

### Prerequisites

- A Phoenix Cloud account + API key
- A Gemini API key (`GOOGLE_API_KEY`) — or Vertex AI via `gcloud` ADC
- [uv](https://docs.astral.sh/uv/) and **Node 20+** (the cockpit UI and the
  Phoenix MCP server both need Node)

Copy `.env.example` to `.env` and fill in `PHOENIX_API_KEY_OBSERVED_WRITE`,
`PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_PROJECT_NAME`, and `GOOGLE_API_KEY`.
Full setup is in [`doc/development.md`](./doc/development.md).

### Launch (one command)

```bash
./scripts/start-cockpit.sh
```

This starts the control-plane API (`:8800`), the trace-ingest server (`:8765`),
and the React cockpit (Vite, `:5173`). **Open http://localhost:5173.**

### What to try

1. The cockpit opens mid-crisis. The Governor reads the fleet's traces,
   **auto-applies** the safe fixes (cache a redundant search loop, cap a runaway
   loop), and **$/message** steps down on the value spine.
2. When a fix is answer-affecting (route to a cheaper model), it **escalates to
   you** — click **arm it**, or open the **debugger** to replay the change across
   N real conversations and see the held/degraded split *before* you commit. The
   eval is a real neutral-judge panel (gemini-2.5-pro, majority vote).
3. Click any agent → **🔍 ask the governor**: the real ADK agent calls the
   **Phoenix MCP server** at runtime to pull a real trace and explain the waste,
   grounded in the spans — trace/span ids link straight into Phoenix.

### Deploy your own

`./infra/deploy.sh` builds and deploys the whole thing as a single Cloud Run
service. See [`infra/README.md`](./infra/README.md).

---

## Demo video

**▶ [Watch the 3-minute demo on YouTube](https://youtu.be/VljIgux2VZ4)**

---

## License

[MIT](./LICENSE). Free for any use, commercial or otherwise. No warranty.

---

## Acknowledgments

Built for the Google Cloud Rapid Agent Hackathon. Thanks to the Google
Cloud, Gemini, and Arize teams for the tooling that made this possible.
