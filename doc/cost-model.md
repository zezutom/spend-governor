# Cost model

How per-trace and per-task-type cost is computed from Phoenix trace
data.

## Principles

- **Auditable.** Every dollar number in the output breaks down into
  its components: which model, which token bucket, which unit rate,
  which tool, how many calls. No magic numbers in the dashboard.
- **Stateless and deterministic.** `cost.py` takes inputs and
  returns outputs. No I/O, no caching, no hidden globals.
- **No hidden defaults.** Adding a new tool without a price in
  `pricing/tools.py` defaults to $0 per call. Adding a new model
  without a price in `pricing/gemini.py` raises `KeyError` —
  surfacing the gap explicitly rather than silently undercounting.

## Gemini token shape

Gemini's `usage_metadata` exposes four fields the cost model cares
about:

```
prompt_token_count          = total input tokens
cached_content_token_count  = subset of input that came from cache
candidates_token_count      = main response output
thoughts_token_count        = reasoning output (Gemini 2.5 thinking)
```

Two non-obvious billing details:

- **Cached input is priced separately** from uncached input, and at
  a much lower rate (Flash: $0.075/M vs $0.30/M; Pro: $0.3125/M vs
  $1.25/M). A naive `total_tokens × input_price` formula will
  overcount cost when caching is active.
- **Thinking tokens are billed at the output rate**, not the input
  rate. They appear in `thoughts_token_count` (or
  `completion_details.reasoning` in OpenInference's trace shape).

`token_usage_from_gemini()` in `cost.py` handles the reshape:

```
uncached_input = prompt_token_count - cached_content_token_count
output         = candidates_token_count + thoughts_token_count
```

## Pricing tables

### Models

`src/accountant/pricing/gemini.py` defines `GEMINI_2_5_FLASH` and
`GEMINI_2_5_PRO` as `ModelPrice` dataclass instances. Each has
three rates: input uncached, input cached, output. Rates are per
1M tokens, in USD.

Source: Vertex AI public pricing.

The Pro entry uses the small-context tier (≤200k input tokens). If
a single call exceeds that ceiling, the cost computation will still
work but the rate will be wrong — add the large-context tier
explicitly when needed.

### Tools

`src/accountant/pricing/tools.py` defines `TOOL_PRICES` — a flat
dict of tool name → per-call USD rate.

The internal Stratus Forms APIs (`kb_lookup`, `customer_lookup`,
`refund_api`, `ticket_update`, `escalate_human`) are priced at
trivial fixed rates that stand in for the marginal cost of a
synchronous internal RPC. `web_search` is priced an order of
magnitude higher to reflect its real-world cost as a third-party
search API. `task_classifier` is local deterministic Python — its
per-call cost is zero.

These are placeholder rates. Replace them with real pricing data
when adapting this to a production setting.

## Computing trace cost

`compute_trace_cost()` is the entry point. It takes:

- `llm_usages`: a list of `(usage_metadata, model_name)` pairs, one
  per LLM call in the trace, in invocation order.
- `tool_calls`: an ordered list of tool names that fired in the
  trace.
- `model_prices`: the `MODELS` dict from `pricing/gemini.py`.
- `tool_prices`: the `TOOL_PRICES` dict from `pricing/tools.py`.

And returns a dict containing:

- `llm_calls`: per-call breakdowns with token counts, rates, and
  USD subtotals
- `tool_calls`: per-call breakdowns with rates and USD subtotals
- `llm_total_usd`, `tool_total_usd`, `total_usd`

## Worked example

A refund trace with the anti-pattern firing typically looks like:

```
tools = [task_classifier, kb_lookup, web_search, web_search,
         web_search, customer_lookup, refund_api, ticket_update]
LLM calls = 8
```

Tool cost breakdown:

- `task_classifier`: $0.000 × 1
- `kb_lookup`: $0.0001 × 1
- `web_search`: $0.005 × 3 = $0.015
- `customer_lookup`: $0.0001 × 1
- `refund_api`: $0.001 × 1
- `ticket_update`: $0.0001 × 1

Tool subtotal: ≈ $0.0163

LLM cost depends on the specific token counts; typical refund
traces add ~$0.008 in LLM cost (Flash at 8 calls, ~10k total
tokens).

Trace total: ≈ $0.024.

A clean `password_reset` trace, by comparison, lands around $0.0037
— roughly a 6× ratio. The dollar gap is the signal the aggregation
layer surfaces.

## Verifying against a known input

`verify_cost.py` runs `compute_llm_cost` against a hand-picked
`usage_metadata` dict and prints the breakdown:

```
uv run python -m accountant.verify_cost
```

Use this as a regression check whenever pricing rates change.
