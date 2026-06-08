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

## 1. Model pricing — Phoenix's built-in defaults are sufficient

For LLM **actual** cost, Phoenix computes from token attributes and a
model pricing table. **Phoenix ships built-in default pricing that
already covers the Gemini 2.5 models the observed agent uses — no manual
`Settings → Models` configuration is required.** Reasoning ("thoughts")
tokens are priced at the output rate by Phoenix automatically.

**Verified 2026-05-30** by reconciliation against Phoenix's GraphQL
`getSpanByOtelId(spanId).costSummary` — uncached-input, output, reasoning
(priced at the output rate), **and cached-input** all match our
`gemini.py` computation to the cent. Phoenix's defaults are correct;
nothing to override.

> **Cached input — required an instrumentation fix.** The OpenInference
> google-adk instrumentor does **not** surface Gemini's cached-input
> tokens, so Phoenix originally priced the whole prompt at the uncached
> rate and overcounted every cache-heavy call by ~50–60% (the agent
> reuses a large system prompt → most tokens are cached). Fixed in
> `cost_after_model_callback` (`wrapper/wrapper.py`), which now emits
> `llm.token_count.prompt_details.cache_read` from the raw usage so
> Phoenix applies the cached rate. **Also corrected:** our `gemini.py`
> cached rates were ~25% of input (wrong); Gemini 2.5+ bills cached input
> at **10% of the uncached rate** (90% discount). After both fixes,
> cache-heavy spans reconcile exactly. (Implicit caching only — no TTL
> storage charge to model.)

### Reference rates (for the audit trail; not something you configure)
Source: <https://cloud.google.com/vertex-ai/generative-ai/pricing>
(Gemini 2.5 section), as of 2026-05-30. Cached input = **10% of uncached
input** for all 2.5+ models. These are what the defaults *should* equal —
use them only to spot-check if a future reconciliation drifts.

| Model | Input (uncached) per 1M | Input (cached, 10%) per 1M | Output per 1M | Notes |
|-------|------------------------|----------------------|---------------|-------|
| `gemini-2.5-flash` | $0.30 | $0.030 | $2.50 | Default observed-agent model |
| `gemini-2.5-flash-lite` | $0.10 | $0.010 | $0.40 | The cheaper tier the wrapper routes simple requests to |
| `gemini-2.5-pro` | $1.25 | $0.125 | $10.00 | Small-context tier (≤200k input tokens); used for Accountant reasoning. **Add a large-context tier if any call exceeds 200k input tokens** — the rate doubles. |

### If a future reconciliation drifts
If a later run shows Phoenix's `cost` disagreeing with our number, only
then touch `Settings → Models`: open the model's **Advanced** pricing and
set the cached-input and reasoning-token rates explicitly to match the
table above (a flat blended input rate is wrong for Gemini 2.5). Re-run
the GraphQL reconciliation to confirm.

### Cross-check against our code
Our local model price table lives in
[`src/accountant/pricing/gemini.py`](../src/accountant/pricing/gemini.py).
It is the **counterfactual engine** — Phoenix can't price calls the
wrapper *prevented* (model swaps, suppressed tool calls), so the wrapper
uses this table to compute baseline/savings. It must stay in agreement
with Phoenix's effective rates (verified 2026-05-30 — they agree). The
worker's INTERIM local "actual" mirror (`src/accountant/pipeline/worker.py`)
also uses it until refactor #2 wires Phoenix as the dashboard's actual
source. If Phoenix's defaults ever change, update this table to match.

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

## 3. Keeping things in sync

Model rates come from **Phoenix's built-in defaults** — you don't
normally edit them. Our `gemini.py` table must agree with those defaults
(verified 2026-05-30). So when prices change:

1. `src/accountant/pricing/gemini.py` — update to match Phoenix's current
   default rates (this is the counterfactual engine).
2. `src/accountant/pricing/tools.py` — tool rates only (Phoenix doesn't
   price tools).
3. Phoenix UI → Settings → Models — **only if** reconciliation shows
   Phoenix's defaults are wrong for a model (rare); override there.

Then re-run `uv run python -m governor.cli.verify_cost` for a token-math
sanity check, and re-run the GraphQL reconciliation
(`getSpanByOtelId(spanId).costSummary` vs our number on the same tokens)
on a recent trace.

If they disagree, the savings number on a span (computed locally at emit
time with our model table) won't match what Phoenix displays for the same
span — and the trust-through-data contract is violated.

---

## 4. Updating this doc

Whenever a rate changes, update:
- The table for that section.
- The "as of" date in the header of section 1.
- The source URL if Google reorganizes its pricing pages.

This doc is the audit trail. Treat changes accordingly.
