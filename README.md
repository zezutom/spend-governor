# Agent Accountant

**AI Runtime FinOps — a runtime economic governor for AI agents. It
stops wasteful AI spend as it happens, and proves the savings from your
own traces.**

A submission to the [Google Cloud Rapid Agent Hackathon](https://rapid-agent.devpost.com/).

---

## What this does

Production AI agents quietly burn money on economically irrational
execution: the same external searches repeated on every ticket, an
over-powered model answering trivial requests, runaway tool loops. The
spend grows; nobody can point at *where* or stop it without an
engineering project.

Agent Accountant is **not** an analytics dashboard. It's a runtime
economic **governor**. It learns where the waste is from your traces,
then intervenes in real time at an inline gateway your agents' traffic
already flows through — capping redundant calls and serving a
semantically-equivalent cache, routing simple requests to a cheaper
model, preventing wasteful loops. **No prompt edits. No source access.
No engineering sprint.** You route traffic through it; it governs spend
at the boundary, and re-measures from traces to prove the cost fell
while quality held.

A worked example from the demo: a customer-support agent runs **three
redundant web searches on every refund ticket** — refunds cost 6× the
baseline. You click **Activate caching**. The governor starts serving
those searches from a semantic cache (only when the new query is
provably equivalent to a prior one), so the real, paid calls stop
firing. Within one batch of new refunds, the dashboard shows — measured
from the traces, not estimated — refund cost dropping from `$0.023` to
`$0.006` per ticket. Nothing in the agent's prompt or code changed.

---

## Why this, not analytics

The value is **autonomous runtime control**, not visibility. Observability
tells you what already happened and leaves the fix to your engineers.
The governor *acts*:

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
   │  LEARNING PLANE            │   │  ENFORCEMENT PLANE (the governor)   │
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

See [`doc/architecture.md`](./doc/architecture.md) for the full design and
[`doc/realtime-pipeline.md`](./doc/realtime-pipeline.md) for the runtime path.

---

## Built with

- [Google ADK](https://github.com/google/adk-python) + [Gemini](https://deepmind.google/technologies/gemini/) (incl. Gemini embeddings for the semantic cache)
- [Arize / Phoenix](https://phoenix.arize.com/) — traces, the learning signal (read via SDK + MCP)
- FastAPI + SQLite (the gateway + store; Cloud Run / a managed queue / BigQuery in production)
- Streamlit (the operator dashboard)
- [uv](https://docs.astral.sh/uv/) · MIT licensed

---

## Repository layout

```
.
├── src/
│   ├── governor/        The runtime enforcement plane (semantic cache,
│   │                    tool interception, model routing, policy store)
│   ├── accountant/      Learning plane: cost model, detection, savings,
│   │                    verification, ingest, dashboard
│   └── observed/        The demo customer-support agent (governed target)
├── examples/            Sample traces and Accountant outputs
├── doc/
│   ├── architecture.md      Two-plane design overview
│   ├── realtime-pipeline.md Ingest, governor, policies, verification
│   ├── observed-agent.md    The Helpdesk Co-Pilot: tools and instruction
│   ├── cost-model.md        How per-trace / per-task-type cost is computed
│   └── development.md       Setup, environment, and common commands
├── LICENSE
├── CLAUDE.md             Guidance for Claude Code in this repo
└── README.md
```

---

## Running it

### Prerequisites

- Google Cloud project with billing + Vertex AI enabled, `gcloud` authenticated (ADC)
- A Phoenix Cloud account + API key
- [uv](https://docs.astral.sh/uv/)

Full setup is in [`doc/development.md`](./doc/development.md).

### Launch (one command)

```bash
uv run streamlit run src/accountant/dashboard.py
```

The dashboard boots the whole stack: it spawns the gateway/ingest server,
imports your trace history from Phoenix on first run, and opens at
`http://localhost:8501`.

### Try it

1. The dashboard leads with **avoidable AI waste ($/mo)** and a
   signal-first breakdown of where the money goes.
2. Click **Activate caching** (and/or **Activate routing**) on a policy card.
3. Send live traffic through the governed agent:
   ```bash
   ACCOUNTANT_INGEST_URL=http://localhost:8765 \
     uv run python -m observed.generate_dataset 40 2
   ```
4. Watch **Saved so far** climb, and each active card flip to
   **"Verified from your traces: $X → $Y per ticket (−Z%)"** — proven,
   not claimed.
5. In Phoenix, filter on `governor.cache_hit` / `governor.model_routed`
   to see exactly which calls were optimized — the cheaper model and the
   suppressed searches, in the system of record.

---

## Demo video

[Link to 3-minute demo on YouTube](https://www.youtube.com/) *(added before submission)*.

---

## License

[MIT](./LICENSE). Free for any use, commercial or otherwise. No warranty.

---

## Acknowledgments

Built for the Google Cloud Rapid Agent Hackathon. Thanks to the Google
Cloud, Gemini, and Arize teams for the tooling that made this possible.
