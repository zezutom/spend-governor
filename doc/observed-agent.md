# The observed agent

The Helpdesk Co-Pilot for Stratus Forms — a fictional SaaS form
builder. Handles inbound customer-support tickets and resolves them
end-to-end using a toolkit of synthetic APIs.

Lives in `src/observed/`.

## Workflow

Every ticket follows three phases:

1. **Classify.** The agent calls `task_classifier` first with the
   raw customer message. The classifier returns one of four task
   types: `password_reset`, `refund_handling`, `plan_change`,
   `account_question`.
2. **Gather.** The agent calls some subset of `kb_lookup`,
   `web_search`, and `customer_lookup` to collect context.
3. **Resolve.** The agent either takes the resolving action
   (`refund_api`, `ticket_update`) or escalates (`escalate_human`).

The workflow is encoded in the agent instruction in `agent.py`.

## Tools

All seven tools are plain Python callables in `src/observed/tools.py`.
ADK introspects their type annotations to build the LLM-visible
parameter schema; their docstrings become the LLM-visible
descriptions.

| Tool | Purpose | Returns |
|------|---------|---------|
| `task_classifier` | Keyword-based intent classification | `{"task_class": ...}` |
| `kb_lookup` | Internal knowledge-base article lookup | `{"article": ...}` or `{"status": "not_found"}` |
| `web_search` | External search (stubbed; returns 4 fake results) | List of result dicts |
| `customer_lookup` | CRM record fetch | Customer record dict |
| `refund_api` | Issue a refund through billing | `{"status": "ok", "refund_id": ..., "amount_usd": ...}` |
| `ticket_update` | Close out the ticket | `{"status": "ok", "ticket_id": ...}` |
| `escalate_human` | Route to a human queue | `{"status": "escalated", "queue": "human-tier-2"}` |

### Why task_classifier is a tool

Classification could be done with a structured-output schema on the
LLM call, but a tool was chosen because:

- Tool calls produce structured spans with both args and return
  value in trace attributes. Downstream aggregation reads
  `task_class` directly from the `task_classifier` span — no text
  parsing.
- Keyword logic is deterministic and stable across runs.
- The four task types are well-separated by obvious keywords; a
  thin classifier is sufficient.

## Intentional anti-pattern

The agent instruction's refund-handling section deliberately
mandates **three redundant `web_search` calls per refund ticket**,
covering "current FTC refund regulations", "SaaS industry refund
norms", and "competitor refund policies". The agent is told to make
these calls even when the customer's message is incomplete and a
clarifying question would normally short-circuit the workflow.

This is not a bug. Do not simplify the refund-procedure paragraph
in `INSTRUCTION` without expecting the cost characteristics of the
trace dataset to change.

The anti-pattern surfaces in two ways at the trace level:

- Three consecutive `web_search` spans per refund trace
- Elevated LLM cost from the extra reasoning turns to plan and
  consume the search results

Other task types (`password_reset`, `account_question`,
`plan_change`) have no mandated tool sequence beyond
"classify → gather as needed → resolve".

## Synthetic environment

Every tool returns hand-built fake data. There are six fictional
customers (`ACME-001`, `GLOBEX-001`, `INITECH-001`, `STARK-001`,
`WONKA-001`, `WAYNE-001`), four KB articles
(`/policies/refunds`, `/account/password-reset`,
`/billing/plan-changes`, `/account/general`), and a fixed set of
plan tiers. No real external APIs are reached.

This keeps the project reproducible (anyone running it sees the
same behavior), avoids leaking real customer data, and means a
malformed prompt can't cause side effects (e.g. a real refund
being issued).

## Trace emission

`telemetry.py` initializes Phoenix OTEL with `auto_instrument=True`,
which means OpenInference's ADK instrumentor wraps every LLM call
and every tool call into a span automatically.

Token counts come from Vertex's `usage_metadata` on the response,
surfaced into OpenInference attributes as:

- `llm.token_count.prompt`
- `llm.token_count.completion`
- `llm.token_count.completion_details.reasoning` (Gemini's "thoughts"
  tokens — billed at the output rate)
- `llm.token_count.prompt_details.cache_read` (when caching is
  active)

The Phoenix project name defaults to `agent-accountant` (override
with `PHOENIX_PROJECT_NAME`).

## Running the agent

```bash
uv run python -m observed.main "I want a refund for last month's charge."
```

See [development.md](./development.md) for environment setup and
the full command catalogue.
