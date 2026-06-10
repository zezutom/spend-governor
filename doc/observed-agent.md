# The observed agents (the fleet)

The Governor watches a **fleet of four specialized agents** for Stratus
Forms — a fictional SaaS form builder. Each agent has one job, one
**signature cost-waste pattern** (a real, intentional anti-pattern in its
instruction), and is fixed by one real Governor lever:

| Agent | Job | Signature waste | Governor's lever |
|-------|-----|-----------------|------------------|
| **Support Co-Pilot** | resolves inbound helpdesk tickets | redundant `web_search` ×3 per ticket | **cache** (output-preserving) |
| **Refund Auditor** | checks refunds against policy | verification loop — same `web_search` ×5 | **cap** (output-preserving) |
| **Sales Assistant** | pricing questions / quotes | one needless `web_search` the KB already covers | **suppress** |
| **Docs Bot** | how-to answers from the docs | premium model on trivial lookups | **route** to a cheaper model (the trap) |

Everything lives in `src/observed/`. The fleet is defined in `fleet.py`;
`tools.py` holds the shared toolkit; `generate_dataset.py` produces the
corpus the whole demo runs on.

## How an agent identifies itself

Each agent stamps its own identity as the trace **grouping key**: its
`task_classifier` tool (built by `_make_classifier(agent_id)` in
`fleet.py`) simply returns `{"task_class": <agent_id>}`. The agent
instruction (`_BASE`) mandates calling `task_classifier` **first, before
any other tool, on every ticket**.

This is why the existing pipeline — detection, cost breakdown, levers, the
cockpit canvas — groups by **agent** with no schema change: downstream
aggregation reads `task_class` straight off the `task_classifier` span,
and for the fleet that value *is* the agent id (`support_copilot`,
`refund_auditor`, `sales_assistant`, `docs_bot`).

## Tools (honest, shared)

All seven tools are plain Python callables in `src/observed/tools.py`,
shared by every fleet agent. ADK introspects their type annotations to
build the LLM-visible parameter schema; their docstrings become the
LLM-visible descriptions. **The tools are honest — the waste lives
entirely in each agent's instruction, never in the tools, and the
Governor never edits the prompts; it enforces policy at the wrapper
boundary.**

| Tool | Purpose | Returns |
|------|---------|---------|
| `task_classifier` | Stamps the handling agent as the grouping key | `{"task_class": <agent_id>}` |
| `kb_lookup` | Internal knowledge-base article lookup | `{"article": ...}` or `{"status": "not_found"}` |
| `web_search` | External search (stubbed; returns fake results) | List of result dicts |
| `customer_lookup` | CRM record fetch | Customer record dict |
| `refund_api` | Issue a refund through billing | `{"status": "ok", "refund_id": ..., "amount_usd": ...}` |
| `ticket_update` | Close out the ticket | `{"status": "ok", "ticket_id": ...}` |
| `escalate_human` | Route to a human queue | `{"status": "escalated", "queue": "human-tier-2"}` |

Each agent's tools are passed through `governor.wrapper.wrap_tools`, and
the agent carries the wrapper's model-routing / cost / trace callbacks —
so cache, cap, suppress, and route all enforce at the boundary the same
way for every agent.

### Why task_classifier is a tool

Classification could ride on a structured-output schema, but a tool was
chosen because tool calls produce structured spans whose args and return
value land in trace attributes — downstream aggregation reads
`task_class` directly from the `task_classifier` span, no text parsing —
and the call is deterministic and stable across runs.

## The intentional anti-patterns

Each agent's instruction deliberately mandates its signature waste. These
are **not bugs** — they're real behavior the agent performs at runtime,
which the Governor detects from the traces and fixes with a lever (never
by editing the text):

- **Support Co-Pilot** — three `web_search` calls per ticket "to
  corroborate", even when `kb_lookup` already answered it.
- **Refund Auditor** — the *same* `web_search` ("current SaaS refund
  regulations") **five times in a row** as a "verification loop".
- **Sales Assistant** — always one `web_search` ("competitor SaaS form
  builder pricing") even though our pricing is already in the KB.
- **Docs Bot** — answers trivial how-to lookups on a premium model
  (`gemini-2.5-flash`); the *trap*, because routing it cheaper measurably
  degrades quality (caught by the neutral-judge eval).

Do not simplify these procedure paragraphs in `fleet.py` without expecting
the cost characteristics of the trace dataset to change.

## Synthetic environment

Every tool returns hand-built fake data. There are six fictional
customers (`ACME-001`, `GLOBEX-001`, `INITECH-001`, `STARK-001`,
`WONKA-001`, `WAYNE-001`), four KB articles (`/policies/refunds`,
`/account/password-reset`, `/billing/plan-changes`, `/account/general`),
and a fixed set of plan tiers. No real external APIs are reached — which
keeps the project reproducible, avoids leaking real customer data, and
means a malformed prompt can't cause side effects (e.g. a real refund).

## Trace emission

`telemetry.py` initializes Phoenix OTEL with `auto_instrument=True`, so
**OpenInference's ADK instrumentor** wraps every LLM call and every tool
call into a span automatically. Token counts come from the response's
`usage_metadata`, surfaced into OpenInference attributes as:

- `llm.token_count.prompt`
- `llm.token_count.completion`
- `llm.token_count.completion_details.reasoning` (Gemini "thoughts"
  tokens — billed at the output rate)
- `llm.token_count.prompt_details.cache_read` (when caching is active)

The Governor wrapper adds the `accountant.*` cost/intervention attributes
(the internal cost-attribution schema). The Phoenix project name defaults
to `agent-accountant` (override with `PHOENIX_PROJECT_NAME`).

### Fan-out to the Governor

`telemetry.py` registers a second span processor alongside Phoenix's when
`GOVERNOR_INGEST_URL` is set: the same spans are posted to the Governor's
`/ingest` endpoint in real time, in addition to going to Phoenix. This is
what lets the cockpit react to each ticket as it happens (see
[realtime-pipeline.md](./realtime-pipeline.md)). Without the env var, the
agent emits to Phoenix only and logs a notice.

## Running

Generate the fleet corpus (what the demo + cockpit use) — `PER_AGENT`
traces for each of the four agents:

```bash
uv run python -m observed.generate_dataset 50 4   # 50 per agent × 4 agents, concurrency 4
```

A single original Helpdesk agent is also kept in `agent.py` /`main.py` for
quick one-off runs (it classifies into the legacy four ticket types rather
than the fleet ids):

```bash
uv run python -m observed.main "I want a refund for last month's charge."
```

See [development.md](./development.md) for environment setup and the full
command catalogue.
