# Instrumentation schema — the `accountant.*` span namespace

The wrapper sits in front of the customer's LLM and tool calls. Every
span it handles is annotated with `accountant.*` attributes (the internal
cost-attribution component's schema) so that
**any savings number the platform reports is independently re-derivable
by querying the customer's own Phoenix traces** — nothing the dashboard
shows depends on private state the customer can't see.

This is the contract for pillar 4 (*trust through data*). If a number on
the dashboard cannot be reproduced from the attributes below + Phoenix's
native cost, that is a bug.

## Cost ownership — Phoenix owns LLM actual, we own tool actual + all baselines

Actual LLM cost is **not written by the wrapper.** Phoenix computes it
natively from token-count attributes (`llm.token_count.prompt_details.cache_read`,
`completion_details.reasoning`, etc.) and a model pricing table configured
in the Phoenix UI. The wrapper does not duplicate that work — for an LLM
span, **read Phoenix's native cost field**.

Actual tool cost has no Phoenix equivalent (Phoenix's pricing table is
for models). The wrapper writes `accountant.cost.actual_usd` on tool
spans from our local `TOOL_PRICES` table.

Counterfactual ("baseline") cost — what the call *would* have cost
without a Governor policy — has no Phoenix equivalent either. The
wrapper writes it when a policy modified the call, along with the
counterfactual parameters used to derive it, so the math is auditable
from the span itself.

See `phoenix-pricing-config.md` for the model and tool pricing tables.

Conventions: USD values are floats rounded to 6 dp. Booleans are only
written when meaningful (a missing boolean reads as false). Token counts
are ints. All values are OTEL-primitive (str / bool / int / float) or
homogeneous string arrays.

---

## Per-span: presence markers (every intercepted span)

| Attribute | Type | When present | Meaning | Phoenix query |
|-----------|------|--------------|---------|---------------|
| `accountant.intercepted` | bool | every span the wrapper handled (tool + LLM) | the call flowed through the wrapper | `accountant.intercepted == true` → everything under governance |
| `accountant.policy_evaluated` | bool | every intercepted span | the wrapper ran policy matching for this call | combine with absent `accountant.modification` → "seen, not modified" |

**Inspected vs modified:** `intercepted == true` AND `modification`
absent ⇒ wrapper saw the call and did nothing. `modification` present ⇒
wrapper modified it.

---

## Per-span: modification (present only when a policy acted)

| Attribute | Type | Meaning |
|-----------|------|---------|
| `accountant.modification` | str enum | one of `cache_hit`, `model_swap`, `tool_swap`, `short_circuit`, `rate_limit` (v1 emits `cache_hit` + `model_swap`; the rest are reserved) |
| `accountant.policy_id` | str | signature of the policy that acted (e.g. `cache_tool:web_search`) |

### `cache_hit` detail (tool spans)
| Attribute | Type | Meaning |
|-----------|------|---------|
| `accountant.cache_hit` | bool | a tool call was served from the semantic cache (no real call) |
| `accountant.cache_similarity` | float | cosine similarity that cleared the equivalence threshold |

### `model_swap` detail (LLM spans)
| Attribute | Type | Meaning |
|-----------|------|---------|
| `accountant.swapped_from` | str | model that would have served the call (= `counterfactual.model`) |
| `accountant.swapped_to` | str | cheaper model that actually served it |

---

## Per-span: cost (the heart of trust-through-data)

The wrapper emits cost attributes **asymmetrically** depending on span
kind, because Phoenix handles only LLM cost.

| Attribute | Span kinds where written | Meaning |
|-----------|--------------------------|---------|
| `accountant.cost.actual_usd` | **tool spans only** | what the tool call actually cost (`TOOL_PRICES[tool]` for executed, `0` for cache hits). On LLM spans, **read Phoenix's native cost field instead** — the wrapper does not emit this. |
| `accountant.cost.baseline_usd` | **modified spans only** | what the call would have cost without the policy. For `cache_hit` → `TOOL_PRICES[counterfactual.tool]`. For `model_swap` → token-by-token cost on the counterfactual model. |
| `accountant.cost.savings_usd` | **modified spans only** | `baseline − actual`. For LLM spans this is computed at emit time using the same pricing table that Phoenix is configured with (see `phoenix-pricing-config.md` for the cross-check). |

For an **unmodified** span, baseline equals actual; nothing is emitted
under `accountant.cost.baseline_usd` / `savings_usd` (Phoenix's native
cost on LLM spans + `accountant.cost.actual_usd` on tool spans give the
full picture; savings are zero by definition).

Trust check — what the dashboard sums for "Saved so far":
`SUM(accountant.cost.savings_usd)` over the window. Independently
verifiable in Phoenix UI by filtering on `accountant.modification` and
summing.

---

## Per-span: counterfactual parameters (modified spans only)

These document the would-have-been parameters used to compute
`baseline_usd`, so the number is reproducible from the span alone.

| Attribute | Type | When present | Meaning |
|-----------|------|--------------|---------|
| `accountant.counterfactual.model` | str | model_swap | the LLM that would have run |
| `accountant.counterfactual.tool` | str | cache_hit | the tool call that would have fired |
| `accountant.counterfactual.prompt_tokens` | int | model_swap | total input tokens (assumed same as the actual call — same prompt) |
| `accountant.counterfactual.completion_tokens` | int | model_swap | output tokens (assumed same shape) |
| `accountant.counterfactual.reasoning_tokens` | int | model_swap, when present | "thoughts" tokens (Gemini 2.5; billed at output rate) |
| `accountant.counterfactual.cache_read_tokens` | int | model_swap, when present | cached-input tokens (priced at the cache_read rate) |
| `accountant.counterfactual.cache_write_tokens` | int | model_swap, when present | cache-write tokens (omitted today — Gemini usage_metadata does not currently expose this; reserved) |

**Assumption (documented, not hidden):** for `model_swap`, the
counterfactual token shape equals the actual token shape. We do not
model whether the original model would have produced a different
response length on the same prompt. Defensible MVP simplification; the
alternative (modelling per-model output length) would itself be a
heuristic.

---

## Per-span: quality (DEFINED, NOT EMITTED in this refactor)

Reserved for the quality-scoring layer (a separate refactor). Namespace
is locked so when the layer ships these attributes appear in known
places.

| Attribute | Type | Meaning |
|-----------|------|---------|
| `accountant.quality.scored` | bool | the output of this call was quality-scored |
| `accountant.quality.score` | float (0–1) | the score |
| `accountant.quality.degraded` | bool | score below the policy's quality floor |

---

## Trace-level rollup (root invocation span, at finalization)

| Attribute | Type | Meaning |
|-----------|------|---------|
| `accountant.trace.savings_usd` | float | Σ `accountant.cost.savings_usd` over the trace's modified spans (so the trace's saving is one read, no join) |
| `accountant.trace.policies_active` | str[] | policy ids active while this trace ran |
| `accountant.trace.quality_degraded` | bool | true if any span in the trace degraded (reserved; false until the quality layer ships) |

Why no `trace.baseline_usd` / `trace.actual_usd` at finalization: Phoenix
has not yet computed LLM actuals at the moment the root span closes, so
totals that include unmodified LLM spans cannot be assembled from the
wrapper alone. Use Phoenix's own trace cost rollup + `trace.savings_usd`
to derive `trace.baseline_usd = phoenix_trace_cost + Σ tool_actuals +
trace.savings_usd` at query time.

---

## Trust-through-data checklist

Every dashboard figure must map to one of these:
- **"Saved so far"** ⇒ `SUM(accountant.cost.savings_usd)`.
- **"Governed LLM spend"** ⇒ `SUM(phoenix.cost)` over `accountant.intercepted == true` LLM spans.
- **"Governed tool spend"** ⇒ `SUM(accountant.cost.actual_usd)` over tool spans.
- **"Calls under cache"** ⇒ `COUNT(accountant.modification == 'cache_hit')`.
- **"Policies live"** ⇒ distinct `accountant.policy_id` (or `trace.policies_active`).

> **Known reconciliation gap (flagged):** the dashboard's realized "Saved
> so far" currently sums the private `accountant_interventions` table,
> not these span attributes. With `accountant.cost.savings_usd` now on
> every modified span, the dashboard should sum spans (via Phoenix
> queries) so the headline is reproducible end-to-end. That wiring is
> part of refactor #2 (cursor pagination); the schema here is the
> precondition.
