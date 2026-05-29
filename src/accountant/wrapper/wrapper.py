"""The wrapper — Agent Accountant's enforcement plane.

Sits in the observed agent's call path (here as in-process tool wrappers
+ ADK callbacks; in production a network gateway the agent's traffic
routes through). On operator-activated policies it intervenes in real
time and annotates every span it handles with the `accountant.*` schema
(see `doc/instrumentation-schema.md`), so savings are independently
verifiable in the customer's own Phoenix traces.

Cost ownership — asymmetric, deliberate:
- **Actual LLM cost** = Phoenix's native `cost` field (derived from the
  Gemini token-count attributes + the Phoenix-configured pricing table).
  The wrapper does NOT write `accountant.cost.actual_usd` on LLM spans.
- **Actual tool cost** = ours; Phoenix has no tool pricing. The wrapper
  writes `accountant.cost.actual_usd` on tool spans from `TOOL_PRICES`.
- **Counterfactual / baseline + savings** = ours, on modified spans
  only, with `accountant.counterfactual.*` documenting the
  would-have-been parameters so the baseline number is auditable from
  the span alone.

Two enforcement hooks:
- **Tool interception:** a cacheable tool (e.g. `web_search`) consults
  the semantic cache before executing. On a semantically-equivalent hit
  the cached result is served and the real (paid) call never fires.
- **Model routing:** simple requests are downgraded to a cheaper model
  via `model_routing_callback` (before_model) +
  `cost_after_model_callback` (after_model, where token counts — hence
  baseline cost — are known).

A per-trace accumulator rolls per-span savings up onto the root span at
finalization. No prompt or source changes — the wrapper only annotates
and substitutes at the boundary.
"""

import contextvars
import functools

from opentelemetry import trace as otel_trace

from accountant.pricing.cost import (
    compute_baseline_llm_cost,
    token_usage_from_gemini,
)
from accountant.pricing.gemini import MODELS
from accountant.pricing.tools import TOOL_PRICES
from accountant.wrapper import store
from accountant.wrapper.cache import SemanticCache


# Default the observed agent would run on if no model_swap applies — the
# counterfactual model for unmodified LLM calls.
DEFAULT_MODEL = "gemini-2.5-flash"

# Per-async-task isolation so concurrent tickets don't bleed into each
# other's contextvars.
_task_class: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_task_class", default=None
)
_route_decision: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_route_decision", default=None
)
# Trace accumulator tracks **savings only**. Total baseline / actual
# can't be assembled at finalization (Phoenix hasn't computed LLM actuals
# by then) — they're recovered at query time from Phoenix's trace cost +
# the per-span `accountant.cost.actual_usd` (tools) + this savings field.
_trace_acc: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_trace_acc", default=None
)

_CACHE = SemanticCache()

# Tools the wrapper can serve from the semantic cache, and the argument
# carrying the cache key.
CACHEABLE: dict[str, str] = {"web_search": "query"}


def current_task_class() -> str | None:
    return _task_class.get()


def _norm_model(name: str | None) -> str:
    if not name:
        return DEFAULT_MODEL
    if "/" in name:
        name = name.split("/", 1)[1]
    return name if name in MODELS else DEFAULT_MODEL


def _accumulate_savings(savings_usd: float) -> None:
    acc = _trace_acc.get()
    if acc is None:
        return
    acc["savings_usd"] += savings_usd


def _annotate_presence(span) -> None:
    """Mark a span as wrapper-handled. Every tool/LLM span the wrapper
    touches gets this — distinguishes wrapper-seen from wrapper-modified
    via the absence/presence of `accountant.modification`."""
    if span is None or not span.is_recording():
        return
    span.set_attribute("accountant.intercepted", True)
    span.set_attribute("accountant.policy_evaluated", True)


def _annotate_tool_actual(span, actual_usd: float) -> None:
    """Tool spans only. Phoenix doesn't price tools, so we own the
    actual-cost number on the span."""
    if span is None or not span.is_recording():
        return
    span.set_attribute("accountant.cost.actual_usd", round(actual_usd, 6))


def _annotate_modification(
    span,
    *,
    modification: str,
    policy_id: str,
    baseline_usd: float,
    savings_usd: float,
    counterfactual: dict | None = None,
    extra: dict | None = None,
) -> None:
    """Written on spans a policy actually modified. Records the
    modification type, the counterfactual parameters used to derive the
    baseline, the baseline cost, the savings, and any modification-
    specific extras (cache_hit bool/similarity, swapped_from/to)."""
    if span is None or not span.is_recording():
        return
    span.set_attribute("accountant.modification", modification)
    if policy_id:
        span.set_attribute("accountant.policy_id", policy_id)
    span.set_attribute("accountant.cost.baseline_usd", round(baseline_usd, 6))
    span.set_attribute("accountant.cost.savings_usd", round(savings_usd, 6))
    for k, v in (counterfactual or {}).items():
        span.set_attribute(f"accountant.counterfactual.{k}", v)
    for k, v in (extra or {}).items():
        span.set_attribute(k, v)


def _parse_task_class(result) -> str | None:
    if isinstance(result, dict):
        # task_classifier returns {"task_class": ...}; ADK may wrap it.
        return result.get("task_class") or (result.get("response") or {}).get("task_class")
    return None


def _active_cache_policy(tool: str) -> dict | None:
    # Match on tool only. The query content scopes the semantic cache;
    # caching a tool globally is the correct, higher-savings behavior.
    for p in store.active_policies():
        if p["policy_type"] != "cache_tool":
            continue
        if p["params"].get("tool") != tool:
            continue
        return p
    return None


def _govern_cacheable(fn, name: str, args, kwargs):
    span = otel_trace.get_current_span()
    _annotate_presence(span)
    baseline = TOOL_PRICES.get(name, 0.0)
    policy = _active_cache_policy(name)
    if not policy:
        # Inspected, not modified: emit actual tool cost.
        _annotate_tool_actual(span, baseline)
        return fn(*args, **kwargs)

    key_arg = CACHEABLE[name]
    query = kwargs.get(key_arg)
    if query is None and args:
        query = args[0]
    if not isinstance(query, str):
        _annotate_tool_actual(span, baseline)
        return fn(*args, **kwargs)

    hit = _CACHE.lookup(name, query)
    if hit is not None:
        # The real (paid) call never executed ⇒ actual cost $0;
        # savings = baseline (the avoided tool unit price).
        _annotate_tool_actual(span, 0.0)
        _annotate_modification(
            span,
            modification="cache_hit",
            policy_id=policy["signature"],
            baseline_usd=baseline,
            savings_usd=baseline,
            counterfactual={"tool": name},
            extra={
                "accountant.cache_hit": True,
                "accountant.cache_similarity": round(hit.similarity, 3),
            },
        )
        _accumulate_savings(baseline)
        store.record_intervention(
            kind="tool_cache_hit",
            tool=name,
            task_class=_task_class.get(),
            cost_avoided_usd=baseline,
            detail={
                "query": query,
                "matched_query": hit.matched_query,
                "similarity": round(hit.similarity, 3),
            },
        )
        return hit.result

    # Cache miss: the policy applied but didn't modify this call.
    _annotate_tool_actual(span, baseline)
    result = fn(*args, **kwargs)
    _CACHE.store(name, query, result)
    return result


def _wrap(fn):
    name = getattr(fn, "__name__", "")

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if name == "task_classifier":
            result = fn(*args, **kwargs)
            tc = _parse_task_class(result)
            if tc:
                _task_class.set(tc)
            span = otel_trace.get_current_span()
            _annotate_presence(span)
            _annotate_tool_actual(span, TOOL_PRICES.get(name, 0.0))
            return result
        if name in CACHEABLE:
            return _govern_cacheable(fn, name, args, kwargs)
        # Non-cacheable tool: inspected, not modified.
        span = otel_trace.get_current_span()
        _annotate_presence(span)
        _annotate_tool_actual(span, TOOL_PRICES.get(name, 0.0))
        return fn(*args, **kwargs)

    return wrapped


def wrap_tools(tools: list) -> list:
    """Wrap each tool callable so its execution flows through the wrapper.
    Harmless when no policy is active — wrapped tools just execute and
    get annotated. `functools.wraps` preserves the signature/docstring
    ADK introspects to build the tool declaration."""
    return [_wrap(t) for t in tools]


# -- Model routing ----------------------------------------------------------

_SIMPLE_MARKERS = (
    "password", "reset my", "can't log in", "cant log in", "can't sign in",
    "locked out", "how do i", "where do i", "where's my", "wheres my",
    "what payment", "can i", "what's the difference", "whats the difference",
)
_COMPLEX_MARKERS = (
    "refund", "money back", "reverse the charge", "charged me", "charge back",
    "upgrade", "downgrade", "change my plan", "switch to", "cancel my subscription",
    "billing cycle", "annual billing",
)


def _ticket_text(llm_request) -> str:
    for content in (llm_request.contents or []):
        if getattr(content, "role", None) == "user":
            for part in (content.parts or []):
                if getattr(part, "text", None):
                    return part.text
    return ""


def _is_simple_request(message: str) -> bool:
    low = message.lower()
    if any(m in low for m in _COMPLEX_MARKERS):
        return False
    return any(m in low for m in _SIMPLE_MARKERS)


def _active_route_policy() -> dict | None:
    for p in store.active_policies():
        if p["policy_type"] == "route_model":
            return p
    return None


def model_routing_callback(callback_context, llm_request):
    """ADK before_model_callback. Decides whether to downgrade this call
    and stashes the decision for `cost_after_model_callback`. No-op
    model change when no route policy is active."""
    original = _norm_model(getattr(llm_request, "model", None))
    decision = {"original": original, "actual": original,
                "swapped": False, "policy_id": None}

    policy = _active_route_policy()
    if policy:
        cheap = policy["params"].get("cheap_model")
        message = _ticket_text(llm_request)
        if cheap and cheap != original and _is_simple_request(message):
            llm_request.model = cheap
            decision.update(actual=cheap, swapped=True,
                            policy_id=policy["signature"],
                            message=message[:80])

    _route_decision.set(decision)
    return None


def cost_after_model_callback(callback_context, llm_response):
    """ADK after_model_callback. Token counts exist here, so this is
    where counterfactual (baseline) LLM cost is computed and the
    `model_swap` modification is recorded. We do NOT write actual cost
    on LLM spans — Phoenix's native `cost` field is canonical for that."""
    span = otel_trace.get_current_span()
    _annotate_presence(span)

    decision = _route_decision.get() or {
        "original": DEFAULT_MODEL, "actual": DEFAULT_MODEL,
        "swapped": False, "policy_id": None,
    }
    _route_decision.set(None)

    if not decision["swapped"]:
        # Inspected, not modified: presence is enough. Phoenix has actual;
        # baseline == actual ⇒ savings 0, nothing more to add.
        return None

    um = getattr(llm_response, "usage_metadata", None)
    if um is None:
        # Modified but no usage to cost the baseline. Emit the
        # modification marker; baseline / savings default to 0.
        _annotate_modification(
            span,
            modification="model_swap",
            policy_id=decision.get("policy_id") or "",
            baseline_usd=0.0,
            savings_usd=0.0,
            counterfactual={"model": decision["original"]},
            extra={
                "accountant.swapped_from": decision["original"],
                "accountant.swapped_to": decision["actual"],
            },
        )
        return None

    prompt_tokens = int(getattr(um, "prompt_token_count", 0) or 0)
    cached_tokens = int(getattr(um, "cached_content_token_count", 0) or 0)
    completion_tokens = int(getattr(um, "candidates_token_count", 0) or 0)
    reasoning_tokens = int(getattr(um, "thoughts_token_count", 0) or 0)

    usage = token_usage_from_gemini({
        "prompt_token_count": prompt_tokens,
        "cached_content_token_count": cached_tokens,
        "candidates_token_count": completion_tokens,
        "thoughts_token_count": reasoning_tokens,
    })
    baseline_usd = compute_baseline_llm_cost(
        usage, MODELS[_norm_model(decision["original"])]
    )["total_usd"]
    # Local computation of actual cost — used ONLY to derive savings_usd
    # at emit time. NOT written to the span: Phoenix's native cost field
    # is canonical for LLM actual cost. The two agree iff Phoenix's
    # pricing config mirrors our table — see doc/phoenix-pricing-config.md
    # for the audit checklist.
    local_actual_usd = compute_baseline_llm_cost(
        usage, MODELS[_norm_model(decision["actual"])]
    )["total_usd"]
    savings_usd = baseline_usd - local_actual_usd

    counterfactual = {
        "model": decision["original"],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if reasoning_tokens:
        counterfactual["reasoning_tokens"] = reasoning_tokens
    if cached_tokens:
        counterfactual["cache_read_tokens"] = cached_tokens
    # cache_write_tokens omitted — Gemini's usage_metadata does not
    # currently expose this; the schema reserves it for when it does.

    _annotate_modification(
        span,
        modification="model_swap",
        policy_id=decision.get("policy_id") or "",
        baseline_usd=baseline_usd,
        savings_usd=savings_usd,
        counterfactual=counterfactual,
        extra={
            "accountant.swapped_from": decision["original"],
            "accountant.swapped_to": decision["actual"],
        },
    )
    _accumulate_savings(savings_usd)
    store.record_intervention(
        kind="model_downgrade",
        tool=None,
        task_class=_task_class.get(),
        cost_avoided_usd=round(savings_usd, 6),
        detail={"from": decision["original"], "to": decision["actual"],
                "message": decision.get("message", "")},
    )
    return None


# -- Trace finalization -----------------------------------------------------


def trace_start_callback(callback_context):
    """ADK before_agent_callback. Open a fresh per-trace savings
    accumulator and snapshot the policies active for this trace."""
    _trace_acc.set({
        "savings_usd": 0.0,
        "policies": [p["signature"] for p in store.active_policies()],
        "quality_degraded": False,
    })
    return None


def trace_finalize_callback(callback_context):
    """ADK after_agent_callback. Roll the per-trace savings onto the root
    invocation span. Trace baseline / actual totals are recovered at
    query time from Phoenix's native trace cost + the per-span
    `accountant.cost.actual_usd` (tool spans) + this savings field —
    see `doc/instrumentation-schema.md`."""
    acc = _trace_acc.get()
    if acc is None:
        return None
    span = otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("accountant.trace.savings_usd", round(acc["savings_usd"], 6))
        span.set_attribute("accountant.trace.quality_degraded", bool(acc["quality_degraded"]))
        if acc["policies"]:
            span.set_attribute("accountant.trace.policies_active", acc["policies"])
    _trace_acc.set(None)
    return None
