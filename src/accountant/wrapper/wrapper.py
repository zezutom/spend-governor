"""The wrapper — Agent Accountant's enforcement plane.

Sits in the observed agent's call path (here as in-process tool wrappers
+ ADK callbacks; in production a network gateway the agent's traffic
routes through). On operator-activated policies it intervenes in real
time and annotates every span it handles with the `accountant.*` schema
(see gitignore/instrumentation-schema.md), so savings are independently
verifiable in the customer's own Phoenix traces.

Two enforcement hooks:
- **Tool interception:** a cacheable tool (e.g. web_search) consults the
  semantic cache before executing. On a semantically-equivalent hit it
  serves the cached result and the real (paid) call never fires. The
  equivalence check is the quality guardrail; different queries execute.
- **Model routing:** simple requests are downgraded to a cheaper model
  via `model_routing_callback` (before_model) + `cost_after_model_callback`
  (after_model, where token counts — hence cost — are known).

A per-trace accumulator rolls per-span savings up onto the root span at
trace finalization. No prompt or source changes — the wrapper only
annotates and substitutes at the boundary.
"""

import contextvars
import functools

from opentelemetry import trace as otel_trace

from accountant.pricing.cost import compute_llm_cost, token_usage_from_gemini
from accountant.pricing.gemini import MODELS
from accountant.pricing.tools import TOOL_PRICES
from accountant.wrapper import store
from accountant.wrapper.cache import SemanticCache


# Model the observed agent runs by default; the baseline for savings on
# LLM calls that weren't swapped.
DEFAULT_MODEL = "gemini-2.5-flash"

# The current ticket's task class, captured when task_classifier runs.
# ContextVars isolate per async task, so concurrent tickets don't bleed.
_task_class: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_task_class", default=None
)
# Routing decision stashed by before_model for the paired after_model.
_route_decision: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_route_decision", default=None
)
# Per-trace accumulator (savings/baseline/actual + active policies),
# reset at trace start, flushed onto the root span at finalization.
_trace_acc: contextvars.ContextVar = contextvars.ContextVar(
    "accountant_trace_acc", default=None
)

_CACHE = SemanticCache()

# Tools the wrapper can serve from the semantic cache, and which argument
# carries the cache key.
CACHEABLE: dict[str, str] = {"web_search": "query"}


def current_task_class() -> str | None:
    return _task_class.get()


def _norm_model(name: str | None) -> str:
    if not name:
        return DEFAULT_MODEL
    if "/" in name:
        name = name.split("/", 1)[1]
    return name if name in MODELS else DEFAULT_MODEL


def _accumulate(baseline: float, actual: float) -> None:
    acc = _trace_acc.get()
    if acc is None:
        return
    acc["baseline_usd"] += baseline
    acc["actual_usd"] += actual
    acc["savings_usd"] += baseline - actual


def _annotate(span, *, baseline: float, actual: float,
              modification: str | None = None, policy_id: str | None = None,
              extra: dict | None = None) -> None:
    """Write the per-span accountant.* schema onto a live span."""
    if span is None or not span.is_recording():
        return
    span.set_attribute("accountant.intercepted", True)
    span.set_attribute("accountant.policy_evaluated", True)
    span.set_attribute("accountant.baseline_usd", round(baseline, 6))
    span.set_attribute("accountant.actual_usd", round(actual, 6))
    span.set_attribute("accountant.savings_usd", round(baseline - actual, 6))
    if modification:
        span.set_attribute("accountant.modification", modification)
    if policy_id:
        span.set_attribute("accountant.policy_id", policy_id)
    for k, v in (extra or {}).items():
        span.set_attribute(k, v)


def _parse_task_class(result) -> str | None:
    if isinstance(result, dict):
        # task_classifier returns {"task_class": ...}; ADK may wrap it.
        return result.get("task_class") or (result.get("response") or {}).get("task_class")
    return None


def _active_cache_policy(tool: str) -> dict | None:
    # Match on tool only. The query content scopes the semantic cache
    # (a "FTC refund regulations" search only arises in refund tickets),
    # and caching a tool globally is the correct, higher-savings behavior.
    for p in store.active_policies():
        if p["policy_type"] != "cache_tool":
            continue
        if p["params"].get("tool") != tool:
            continue
        return p
    return None


def _govern_cacheable(fn, name: str, args, kwargs):
    span = otel_trace.get_current_span()
    baseline = TOOL_PRICES.get(name, 0.0)
    policy = _active_cache_policy(name)
    if not policy:
        # Inspected, not modified: still annotate so it's distinguishable
        # from a span the wrapper never saw. baseline == actual ⇒ $0 saved.
        _annotate(span, baseline=baseline, actual=baseline)
        _accumulate(baseline, baseline)
        return fn(*args, **kwargs)

    key_arg = CACHEABLE[name]
    query = kwargs.get(key_arg)
    if query is None and args:
        query = args[0]
    if not isinstance(query, str):
        _annotate(span, baseline=baseline, actual=baseline)
        _accumulate(baseline, baseline)
        return fn(*args, **kwargs)

    hit = _CACHE.lookup(name, query)
    if hit is not None:
        # The real (paid) call never executed ⇒ actual cost $0.
        _annotate(
            span, baseline=baseline, actual=0.0,
            modification="cache_hit", policy_id=policy["signature"],
            extra={
                "accountant.cache_hit": True,
                "accountant.cache_similarity": round(hit.similarity, 3),
            },
        )
        _accumulate(baseline, 0.0)
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
    _annotate(span, baseline=baseline, actual=baseline,
              policy_id=policy["signature"])
    _accumulate(baseline, baseline)
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
            # Annotate the classifier span too (inspected, not modified).
            _annotate(otel_trace.get_current_span(),
                      baseline=TOOL_PRICES.get(name, 0.0),
                      actual=TOOL_PRICES.get(name, 0.0))
            _accumulate(TOOL_PRICES.get(name, 0.0), TOOL_PRICES.get(name, 0.0))
            return result
        if name in CACHEABLE:
            return _govern_cacheable(fn, name, args, kwargs)
        # Non-cacheable tool: inspected, not modified.
        span = otel_trace.get_current_span()
        baseline = TOOL_PRICES.get(name, 0.0)
        _annotate(span, baseline=baseline, actual=baseline)
        _accumulate(baseline, baseline)
        return fn(*args, **kwargs)

    return wrapped


def wrap_tools(tools: list) -> list:
    """Wrap each tool callable so its execution flows through the wrapper.
    Harmless when no policy is active — wrapped tools just execute and get
    annotated. functools.wraps preserves the signature/docstring ADK
    introspects to build the tool declaration."""
    return [_wrap(t) for t in tools]


# -- Model routing ----------------------------------------------------------
#
# Downgrade economically simple requests to a cheaper model. ADK honors a
# model set on the LlmRequest in before_model_callback, so we reroute
# there; the cost is computed in after_model_callback, where token counts
# are known. Complexity is judged from the ticket text (a stand-in for a
# general complexity classifier a real gateway would run — it must not
# depend on the customer's task taxonomy).

# refund_handling and plan_change (money / decisions) stay on the stronger
# model; these markers route to the cheaper tier.
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
    and stashes the decision for cost_after_model_callback. Always records
    a decision so every LLM call gets annotated. No-op model change when
    no route policy is active."""
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
    """ADK after_model_callback. Token counts are available here, so this
    is where LLM-call cost (baseline vs actual) is computed and written
    onto the span, and a model-swap intervention is recorded."""
    decision = _route_decision.get() or {
        "original": DEFAULT_MODEL, "actual": DEFAULT_MODEL,
        "swapped": False, "policy_id": None,
    }
    span = otel_trace.get_current_span()

    um = getattr(llm_response, "usage_metadata", None)
    if um is None:
        # No usage to cost; still mark the call as wrapper-seen.
        _annotate(span, baseline=0.0, actual=0.0)
        _route_decision.set(None)
        return None

    usage = token_usage_from_gemini({
        "prompt_token_count": getattr(um, "prompt_token_count", 0) or 0,
        "cached_content_token_count": getattr(um, "cached_content_token_count", 0) or 0,
        "candidates_token_count": getattr(um, "candidates_token_count", 0) or 0,
        "thoughts_token_count": getattr(um, "thoughts_token_count", 0) or 0,
    })
    actual_usd = compute_llm_cost(usage, MODELS[_norm_model(decision["actual"])])["total_usd"]
    baseline_usd = compute_llm_cost(usage, MODELS[_norm_model(decision["original"])])["total_usd"]

    if decision["swapped"]:
        _annotate(
            span, baseline=baseline_usd, actual=actual_usd,
            modification="model_swap", policy_id=decision.get("policy_id"),
            extra={
                "accountant.swapped_from": decision["original"],
                "accountant.swapped_to": decision["actual"],
            },
        )
        store.record_intervention(
            kind="model_downgrade",
            tool=None,
            task_class=_task_class.get(),
            cost_avoided_usd=round(baseline_usd - actual_usd, 6),
            detail={"from": decision["original"], "to": decision["actual"],
                    "message": decision.get("message", "")},
        )
    else:
        _annotate(span, baseline=baseline_usd, actual=actual_usd)

    _accumulate(baseline_usd, actual_usd)
    _route_decision.set(None)
    return None


# -- Trace finalization -----------------------------------------------------


def trace_start_callback(callback_context):
    """ADK before_agent_callback. Open a fresh per-trace accumulator and
    snapshot the policies active for this trace."""
    _trace_acc.set({
        "baseline_usd": 0.0,
        "actual_usd": 0.0,
        "savings_usd": 0.0,
        "policies": [p["signature"] for p in store.active_policies()],
        "quality_degraded": False,
    })
    return None


def trace_finalize_callback(callback_context):
    """ADK after_agent_callback. Roll the per-trace totals up onto the
    root invocation span so a single span carries the trace's economics."""
    acc = _trace_acc.get()
    if acc is None:
        return None
    span = otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("accountant.trace_baseline_usd", round(acc["baseline_usd"], 6))
        span.set_attribute("accountant.trace_actual_usd", round(acc["actual_usd"], 6))
        span.set_attribute("accountant.trace_savings_usd", round(acc["savings_usd"], 6))
        span.set_attribute("accountant.trace_quality_degraded", bool(acc["quality_degraded"]))
        if acc["policies"]:
            span.set_attribute("accountant.policies_active", acc["policies"])
    _trace_acc.set(None)
    return None
