"""Turn raw detector output into actionable, costed issues.

Two jobs:

1. **Dedupe.** A single root cause often trips multiple detectors — a
   refund's redundant web searches show up as BOTH a class_cost_uplift
   AND a repeated_tool anomaly. This collapses all anomalies for a task
   class into ONE issue, so the operator sees one card per problem, not
   three cards for the same problem.

2. **Price the fix.** Each issue carries a projected savings: per-ticket
   $ and %, plus a monthly $ projection extrapolated from the observed
   traffic. Every figure decomposes into auditable components (calls
   removed × unit price, proportional LLM share) — no magic numbers.

Savings model for a repeated-tool issue: the fix removes the redundant
tool calls. Savings per ticket =
    (avg calls of that tool) × (tool unit price)        # tool cost
  + (avg LLM cost) × (those tool-steps / all tool-steps) # LLM share
The LLM term reflects that each tool call carries a reasoning turn.
This is conservative and self-checking: for refunds it projects a
post-fix cost close to the clean-task baseline.
"""

from collections import defaultdict

from accountant.pricing.tools import TOOL_PRICES


def _issue_signature(task_class: str) -> str:
    return f"issue:{task_class}"


def _build_issue(
    task_class: str,
    summary: dict,
    repeated: list[dict],
    uplift: dict | None,
    window_days: float,
) -> dict:
    n = summary["n"]
    current_avg = summary["avg_cost_usd"]
    avg_tools = summary.get("avg_tools") or 0
    avg_llm = summary.get("avg_llm_cost_usd") or 0.0
    avg_tool_counts = summary.get("avg_tool_counts") or {}

    # The actionable lever: the most expensive repeated tool (calls ×
    # unit price × how many traces hit it).
    primary = None
    if repeated:
        primary = max(
            repeated,
            key=lambda a: TOOL_PRICES.get(a["tool"], 0.0) * a.get("traces_with_repeat", 0),
        )

    tool_savings = 0.0
    llm_savings = 0.0
    primary_tool = None
    avg_repeats = 0.0
    if primary:
        primary_tool = primary["tool"]
        avg_repeats = avg_tool_counts.get(primary_tool, 0.0)
        unit_price = TOOL_PRICES.get(primary_tool, 0.0)
        tool_savings = avg_repeats * unit_price
        if avg_tools > 0:
            llm_savings = avg_llm * min(avg_repeats / avg_tools, 1.0)

    savings_per_ticket = round(tool_savings + llm_savings, 6)
    projected_avg = max(round(current_avg - savings_per_ticket, 6), 0.0)
    pct = round(savings_per_ticket / current_avg, 3) if current_avg else 0.0

    monthly_volume = round(n / window_days * 30.0) if window_days else n
    monthly_savings = round(savings_per_ticket * monthly_volume, 2)

    return {
        "signature": _issue_signature(task_class),
        "task_class": task_class,
        "n_traces": n,
        "current_avg_usd": current_avg,
        "projected_avg_usd": projected_avg,
        "savings_per_ticket_usd": savings_per_ticket,
        "pct_reduction": pct,
        "monthly_volume": monthly_volume,
        "monthly_savings_usd": monthly_savings,
        "primary_tool": primary_tool,
        "avg_repeats": round(avg_repeats, 2),
        "uplift_x": uplift["uplift_x"] if uplift else None,
        "components": {
            "tool_savings_per_ticket_usd": round(tool_savings, 6),
            "llm_savings_per_ticket_usd": round(llm_savings, 6),
            "tool_unit_price_usd": TOOL_PRICES.get(primary_tool, 0.0) if primary_tool else 0.0,
            "avg_tool_calls_removed": round(avg_repeats, 2),
            "window_days": round(window_days, 2),
        },
        "anomalies": repeated + ([uplift] if uplift else []),
        "actionable": savings_per_ticket > 0,
    }


# Task classes simple enough to run on the cheaper model tier.
SIMPLE_CLASSES = ("password_reset", "account_question")
# Fraction of LLM cost retained after routing to flash-lite. These
# tasks are input-heavy and flash-lite input is ~3x cheaper, so we keep
# a conservative ~35% (a ~65% LLM reduction). Labelled an estimate; the
# realized number comes from post-routing traces.
ROUTING_LLM_RETAIN_RATIO = 0.35
CHEAP_MODEL = "gemini-2.5-flash-lite"


def _model_routing_issue(by_task_class: dict, window_days: float) -> dict | None:
    classes = [tc for tc in SIMPLE_CLASSES if tc in by_task_class]
    if not classes:
        return None
    n = sum(by_task_class[tc]["n"] for tc in classes)
    if n == 0:
        return None
    cur_llm = sum(by_task_class[tc]["avg_llm_cost_usd"] * by_task_class[tc]["n"] for tc in classes) / n
    cur_total = sum(by_task_class[tc]["avg_cost_usd"] * by_task_class[tc]["n"] for tc in classes) / n
    savings_per_ticket = round(cur_llm * (1 - ROUTING_LLM_RETAIN_RATIO), 6)
    projected = max(round(cur_total - savings_per_ticket, 6), 0.0)
    pct = round(savings_per_ticket / cur_total, 3) if cur_total else 0.0
    monthly_volume = round(n / window_days * 30.0) if window_days else n
    monthly = round(savings_per_ticket * monthly_volume, 2)
    return {
        "signature": "route_model:simple",
        "kind": "model_routing",
        "task_class": "simple requests",
        "n_traces": n,
        "current_avg_usd": round(cur_total, 6),
        "projected_avg_usd": projected,
        "savings_per_ticket_usd": savings_per_ticket,
        "pct_reduction": pct,
        "monthly_volume": monthly_volume,
        "monthly_savings_usd": monthly,
        "primary_tool": None,
        "cheap_model": CHEAP_MODEL,
        "classes": classes,
        "components": {
            "current_llm_per_ticket_usd": round(cur_llm, 6),
            "llm_cost_retained_ratio": ROUTING_LLM_RETAIN_RATIO,
            "cheap_model": CHEAP_MODEL,
            "window_days": round(window_days, 2),
        },
        "actionable": savings_per_ticket > 0,
    }


def build_issues(
    by_task_class: dict,
    anomalies: list[dict],
    window_days: float,
) -> list[dict]:
    """Collapse anomalies into one issue per task class (costed), plus a
    model-routing issue for simple classes. Sorted by monthly savings
    (biggest leak first)."""
    by_tc: dict[str, list] = defaultdict(list)
    for a in anomalies:
        tc = a.get("task_class")
        if tc and tc != "unknown":
            by_tc[tc].append(a)

    issues = []
    for tc, anoms in by_tc.items():
        summary = by_task_class.get(tc)
        if not summary:
            continue
        repeated = [a for a in anoms if a.get("type") == "repeated_tool"]
        uplift = next((a for a in anoms if a.get("type") == "class_cost_uplift"), None)
        issue = _build_issue(tc, summary, repeated, uplift, window_days)
        issue["kind"] = "tool_cache"
        issues.append(issue)

    routing = _model_routing_issue(by_task_class, window_days)
    if routing:
        issues.append(routing)

    issues.sort(key=lambda i: i.get("monthly_savings_usd", 0.0), reverse=True)
    return issues
