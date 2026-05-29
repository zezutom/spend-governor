# Pricing configuration — the audit reference

Two pricing tables drive every cost number the platform reports. They
must stay in sync; if they ever diverge, savings claims diverge from
Phoenix's native cost and the trust-through-data contract breaks.

| What | Source of truth | Used for |
|------|-----------------|----------|
| Model pricing | **Phoenix UI** (Settings → Models) | Phoenix's native LLM `cost` field — the actual cost of every LLM call |
| Tool pricing | **Code** — `src/accountant/pricing/tools.py` (`TOOL_PRICES`) | `accountant.cost.actual_usd` on tool spans (Phoenix doesn't price tools) |
| Counterfactual / baseline | **Code** — `src/accountant/pricing/gemini.py` (mirrors the Phoenix UI) + `TOOL_PRICES` | `accountant.cost.baseline_usd` on modified spans |

If anyone asks why a cost number is what it is, the answer is in this
doc.

---

## 1. Model pricing — configure in the Phoenix UI

For LLM **actual** cost, Phoenix computes from token attributes and the
pricing table you configure in **Settings → Models**. Set the rates
below for each model the observed agent might call. **Use Phoenix's
"Advanced" pricing to enter the cached-input and reasoning-token rates
separately** — a single blended input rate is wrong for Gemini 2.5
because cached input is priced much lower than uncached, and reasoning
("thoughts") tokens are billed at the output rate, not the input rate.

### Rates as of 2026-05-23
Source: <https://cloud.google.com/vertex-ai/generative-ai/pricing>
(Gemini 2.5 section). Re-verify when running for the demo / submission.

| Model | Input (uncached) per 1M | Input (cached) per 1M | Output per 1M | Notes |
|-------|------------------------|----------------------|---------------|-------|
| `gemini-2.5-flash` | $0.30 | $0.075 | $2.50 | Default observed-agent model |
| `gemini-2.5-flash-lite` | $0.10 | $0.025 | $0.40 | The cheaper tier the wrapper routes simple requests to |
| `gemini-2.5-pro` | $1.25 | $0.3125 | $10.00 | Small-context tier (≤200k input tokens); used for Accountant reasoning. **Add a large-context tier if any call exceeds 200k input tokens** — the rate doubles. |

### Per-component rates (for Phoenix Advanced pricing)
- **Reasoning ("thoughts") tokens** → priced at the **output rate** above.
- **Cache-read tokens** → the **input (cached)** rate above.
- **Cache-write tokens** → currently 0 in our calculation (Gemini's
  `usage_metadata` does not yet expose this distinctly). If Phoenix's UI
  asks for a cache-write rate, leave it consistent with Google's published
  cache-write pricing for that model.

### Configuration checklist (manual, in Phoenix Cloud)
- [ ] Settings → Models → add `gemini-2.5-flash` with the rates above.
- [ ] Add `gemini-2.5-flash-lite` with the rates above.
- [ ] Add `gemini-2.5-pro` (small-context tier) with the rates above.
- [ ] For each, open **Advanced** pricing and set the cached-input rate
      and the reasoning-token rate explicitly. A flat blended rate will
      under- or over-price every cached / thinking-token-heavy call.
- [ ] Sanity-check: run a known prompt through the observed agent, note
      the token counts on the LLM span in Phoenix, and verify the `cost`
      field equals `tokens × rates` from this table.

### Cross-check against our code
Our local model price table lives in
[`src/accountant/pricing/gemini.py`](../src/accountant/pricing/gemini.py).
The numbers MUST match the Phoenix UI — the wrapper uses this same table
to compute counterfactual (baseline) cost, and the local "actual" mirror
in the worker (see `src/accountant/pipeline/worker.py`, marked INTERIM)
uses it until refactor #2 wires Phoenix as the dashboard's actual source.
If you change one, change the other.

---

## 2. Tool pricing — lives in code

Phoenix's pricing table is for models. Tool calls have a flat per-call
rate that we set ourselves. The table:

[`src/accountant/pricing/tools.py`](../src/accountant/pricing/tools.py)
defines `TOOL_PRICES`:

| Tool | Per-call USD | Rationale |
|------|--------------|-----------|
| `task_classifier` | $0 | Local deterministic Python; no I/O, no LLM |
| `kb_lookup` | $0.0001 | Internal RPC stand-in (vector lookup) |
| `customer_lookup` | $0.0001 | Internal CRM RPC stand-in |
| `ticket_update` | $0.0001 | Internal ticketing RPC stand-in |
| `escalate_human` | $0.0001 | Routing handoff |
| `refund_api` | $0.001 | Internal billing API; mild floor above the trivial rate to reflect a payments call |
| `web_search` | $0.005 | The only externally-priced tool — chosen an order of magnitude higher than internal RPCs so the refund anti-pattern (3× `web_search` per ticket) shows up as a clear cost delta |

These are **placeholder defaults for the demo**, not vendor rates —
they exist so the trace economics tell a coherent story. Adjust to
real per-tool pricing if you deploy against a customer's actual
toolkit.

### Where these numbers flow
- The wrapper writes `accountant.cost.actual_usd = TOOL_PRICES[name]`
  on every executed tool span (and `= 0` on cache-hit spans).
- The wrapper writes `accountant.cost.baseline_usd = TOOL_PRICES[name]`
  on cache-hit spans (the counterfactual: what the bypassed call
  would have cost).
- The CLI audit tools (`inspect_traces.py`, `verify_cost.py`) re-derive
  totals from this table for cross-checking.

---

## 3. Keeping the two in sync

When updating prices, do it in **both places**:

1. Phoenix UI → Settings → Models (model rates).
2. `src/accountant/pricing/gemini.py` (model rates, mirrored).
3. `src/accountant/pricing/tools.py` (tool rates only).

Then re-run `uv run python -m accountant.cli.verify_cost` for a sanity
check that token math hasn't drifted, and visit a recent trace in
Phoenix to confirm Phoenix's `cost` field agrees with our local mirror
on the same tokens.

If they ever disagree, the savings number on a span (computed locally
at emit time with our model table) will not match what Phoenix displays
for the same span — and the trust-through-data contract is violated.

---

## 4. Updating this doc

Whenever a rate changes, update:
- The table for that section.
- The "as of" date in the header of section 1.
- The source URL if Google reorganizes its pricing pages.

This doc is the audit trail. Treat changes accordingly.
