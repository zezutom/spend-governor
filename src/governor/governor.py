"""Runtime governor — the enforcement plane.

Sits in the observed agent's call path (here as in-process tool
wrappers; in production a network gateway the agent's traffic routes
through). On operator-activated policies it intervenes in real time:

- **Tool interception:** a governed tool (e.g. web_search) consults the
  semantic cache before executing. On a semantically-equivalent hit it
  serves the cached result and the real (expensive) call never fires —
  recording the cost avoided. The equivalence check is the quality
  guardrail; genuinely different queries still execute.

Model routing (downgrade simple requests to a cheaper model) is the
second enforcement hook and plugs in via `before_model_callback`.

No prompt or source changes — the governor only wraps the boundary.
"""

import contextvars
import functools

from opentelemetry import trace as otel_trace

from governor import store
from governor.cache import SemanticCache


# The current ticket's task class, captured when task_classifier runs,
# so tool-governance policies can be scoped per task type. ContextVars
# isolate per async task, so concurrent tickets don't bleed into each
# other.
_task_class: contextvars.ContextVar = contextvars.ContextVar(
    "governor_task_class", default=None
)

_CACHE = SemanticCache()

# Tools the governor can serve from the semantic cache, and which
# argument carries the cache key.
CACHEABLE: dict[str, str] = {"web_search": "query"}


def current_task_class() -> str | None:
    return _task_class.get()


def _parse_task_class(result) -> str | None:
    if isinstance(result, dict):
        # task_classifier returns {"task_class": ...}; ADK may wrap it.
        return result.get("task_class") or (result.get("response") or {}).get("task_class")
    return None


def _active_cache_policy(tool: str) -> dict | None:
    # Match on tool only. Per-task-class scoping isn't enforced here:
    # ADK runs each tool call in its own execution context, so the
    # class captured in task_classifier doesn't reliably propagate to
    # the web_search call. It isn't needed anyway — the query content
    # scopes the semantic cache (a "FTC refund regulations" search only
    # arises in refund tickets), and caching a tool globally is the
    # correct, higher-savings behavior.
    for p in store.active_policies():
        if p["policy_type"] != "cache_tool":
            continue
        if p["params"].get("tool") != tool:
            continue
        return p
    return None


def _govern_cacheable(fn, name: str, args, kwargs):
    policy = _active_cache_policy(name)
    if not policy:
        return fn(*args, **kwargs)

    key_arg = CACHEABLE[name]
    query = kwargs.get(key_arg)
    if query is None and args:
        query = args[0]
    if not isinstance(query, str):
        return fn(*args, **kwargs)

    hit = _CACHE.lookup(name, query)
    if hit is not None:
        # Mark the tool's trace span so the Accountant prices this call
        # at $0 — the real (paid) call never executed. This keeps the
        # trace-measured cost honest about caching, not just the
        # governor's own intervention log.
        span = otel_trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("governor.cache_hit", True)
            span.set_attribute("governor.cache_similarity", round(hit.similarity, 3))
        store.record_intervention(
            kind="tool_cache_hit",
            tool=name,
            task_class=_task_class.get(),
            cost_avoided_usd=float(policy["params"].get("cost_per_call_usd", 0.0)),
            detail={
                "query": query,
                "matched_query": hit.matched_query,
                "similarity": round(hit.similarity, 3),
            },
        )
        return hit.result

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
            return result
        if name in CACHEABLE:
            return _govern_cacheable(fn, name, args, kwargs)
        return fn(*args, **kwargs)

    return wrapped


def govern_tools(tools: list) -> list:
    """Wrap each tool callable so its execution flows through the
    governor. Harmless when no policy is active — governed tools just
    execute normally. functools.wraps preserves the signature/docstring
    ADK introspects to build the tool declaration."""
    return [_wrap(t) for t in tools]


# -- Model routing ----------------------------------------------------------
#
# Second enforcement hook: downgrade economically simple requests to a
# cheaper model. ADK honors a model set on the LlmRequest in
# before_model_callback, so we reroute there. Complexity is judged from
# the ticket text with the same keyword signal the observed agent uses
# (a stand-in for a general complexity classifier a real gateway would
# run — it must not depend on the customer's task taxonomy).

# Requests in these classes are simple enough for the cheaper tier;
# refund_handling and plan_change (money / decisions) stay on the
# stronger model.
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
    """ADK before_model_callback. On an active route_model policy,
    reroute simple requests to the cheaper model. No-op otherwise."""
    policy = _active_route_policy()
    if not policy:
        return None
    cheap = policy["params"].get("cheap_model")
    if not cheap:
        return None
    message = _ticket_text(llm_request)
    if not _is_simple_request(message):
        return None
    original = getattr(llm_request, "model", None)
    if original == cheap:
        return None
    llm_request.model = cheap
    # Tag the span so the routed call is filterable in Phoenix
    # (e.g. filter governor.model_routed == true).
    span = otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("governor.model_routed", True)
        span.set_attribute("governor.routed_from", original or "")
        span.set_attribute("governor.routed_to", cheap)
    store.record_intervention(
        kind="model_downgrade",
        tool=None,
        task_class=None,
        cost_avoided_usd=float(policy["params"].get("est_savings_per_call_usd", 0.0)),
        detail={"from": original, "to": cheap, "message": message[:80]},
    )
    return None
