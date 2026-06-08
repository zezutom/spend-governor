"""Counterfactual (baseline) cost computation.

**Phoenix owns the actual cost of LLM calls** — it derives `cost` from
token-count attributes + a model pricing table configured in the Phoenix
UI. We don't duplicate that work.

This module is the primitive for the OTHER cost number the product needs:
the COUNTERFACTUAL — what an LLM call WOULD have cost without an
Governor policy applied (e.g. on the original, pre-routed model). The
wrapper writes `accountant.cost.baseline_usd` and
`accountant.cost.savings_usd` using these functions; the schema doc
(`doc/instrumentation-schema.md`) is the contract.

Also used by:
- the audit CLI tools (`inspect_traces.py`, `verify_cost.py`) to recompute
  totals from token data as a cross-check against Phoenix;
- the worker, **interim**, as a local mirror of actual LLM cost while the
  dashboard still reads SQLite — to be retired when refactor #2 wires
  Phoenix as the dashboard's actual-cost source.

Token-counting conventions follow Gemini 2.5's usage_metadata shape:

    prompt_token_count          = total input
    cached_content_token_count  = subset of prompt that came from cache
    candidates_token_count      = main output
    thoughts_token_count        = reasoning output (billed at output rate)

Cached input is priced separately and lower than uncached input on
Gemini 2.5. Thinking tokens are billed at the output rate, not the input
rate. A naive `total_tokens × input_price` misses both.

Every dict returned exposes the token bucket and the unit rate that
produced each cost component, so any number is auditable.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenUsage:
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelPrice:
    name: str
    input_uncached_per_1m_usd: float
    input_cached_per_1m_usd: float
    output_per_1m_usd: float


def token_usage_from_gemini(usage_metadata: dict) -> TokenUsage:
    """Build a TokenUsage from a Gemini API usage_metadata dict.

    Missing fields are treated as zero — older Gemini responses may omit
    cached_content_token_count or thoughts_token_count entirely.
    """
    prompt = usage_metadata.get("prompt_token_count", 0)
    cached = usage_metadata.get("cached_content_token_count", 0)
    candidates = usage_metadata.get("candidates_token_count", 0)
    thoughts = usage_metadata.get("thoughts_token_count", 0)
    return TokenUsage(
        uncached_input_tokens=prompt - cached,
        cached_input_tokens=cached,
        output_tokens=candidates + thoughts,
    )


def compute_baseline_llm_cost(usage: TokenUsage, price: ModelPrice) -> dict:
    """Compute counterfactual (baseline) LLM cost from token usage + a
    model price. Phoenix is the source of truth for ACTUAL LLM cost; this
    function answers "what would this call have cost on a different
    model?" — the wrapper uses it for `accountant.cost.baseline_usd`.

    Audit tools and the worker's interim actual-cost mirror also call
    this with the actually-used model's price; see module docstring."""
    input_uncached_usd = usage.uncached_input_tokens * price.input_uncached_per_1m_usd / 1_000_000
    input_cached_usd = usage.cached_input_tokens * price.input_cached_per_1m_usd / 1_000_000
    output_usd = usage.output_tokens * price.output_per_1m_usd / 1_000_000
    return {
        "model": price.name,
        "input_uncached_tokens": usage.uncached_input_tokens,
        "input_uncached_rate_per_1m_usd": price.input_uncached_per_1m_usd,
        "input_uncached_usd": input_uncached_usd,
        "input_cached_tokens": usage.cached_input_tokens,
        "input_cached_rate_per_1m_usd": price.input_cached_per_1m_usd,
        "input_cached_usd": input_cached_usd,
        "output_tokens": usage.output_tokens,
        "output_rate_per_1m_usd": price.output_per_1m_usd,
        "output_usd": output_usd,
        "total_usd": input_uncached_usd + input_cached_usd + output_usd,
    }


def compute_tool_cost(tool_name: str, tool_prices: dict[str, float]) -> dict:
    per_call_usd = tool_prices.get(tool_name, 0.0)
    return {
        "tool": tool_name,
        "per_call_usd": per_call_usd,
        "total_usd": per_call_usd,
    }


def compute_trace_cost(
    llm_usages: list[tuple[dict, str]],
    tool_calls: list[str],
    model_prices: dict[str, ModelPrice],
    tool_prices: dict[str, float],
) -> dict:
    """Aggregate per-trace cost from raw LLM and tool-call data.

    Args:
        llm_usages: list of (gemini_usage_metadata, model_name) pairs,
            one entry per LLM call in the trace, in invocation order.
        tool_calls: ordered list of tool names that fired in the trace.
        model_prices: map of model name to ModelPrice.
        tool_prices: map of tool name to per-call USD rate.

    Returns a dict containing the per-call breakdowns and the totals.
    Raises KeyError if a model in llm_usages is not in model_prices —
    tool prices default to 0.0 for unknown tools so adding a new tool
    doesn't crash the dashboard before the price table catches up.
    """
    llm_breakdowns = []
    for usage_metadata, model_name in llm_usages:
        if model_name not in model_prices:
            raise KeyError(f"no price configured for model: {model_name}")
        usage = token_usage_from_gemini(usage_metadata)
        llm_breakdowns.append(compute_baseline_llm_cost(usage, model_prices[model_name]))

    tool_breakdowns = [compute_tool_cost(name, tool_prices) for name in tool_calls]

    llm_total = sum(b["total_usd"] for b in llm_breakdowns)
    tool_total = sum(b["total_usd"] for b in tool_breakdowns)

    return {
        "llm_calls": llm_breakdowns,
        "tool_calls": tool_breakdowns,
        "llm_total_usd": llm_total,
        "tool_total_usd": tool_total,
        "total_usd": llm_total + tool_total,
    }
