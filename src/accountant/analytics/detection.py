"""Tier 1 statistical detection over the spans cache.

Reads the spans table, derives per-trace cost and tool sequence,
aggregates by task_class, and detects two kinds of anomaly:

- class_cost_uplift: a task_class's avg cost is >= UPLIFT_THRESHOLD_X
  of the baseline task_class's avg cost.
- repeated_tool: a single tool fires >= REPEAT_THRESHOLD times within
  a trace, in at least REPEAT_HIT_RATE of that class's traces.

The function returns a state dict that downstream consumers
(recommendations.py, Tier-3 reasoning) read.
"""

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from accountant.pipeline.db import connect


UPLIFT_THRESHOLD_X = 2.0
REPEAT_THRESHOLD = 3
REPEAT_HIT_RATE = 0.10
BASELINE_CLASS = "password_reset"


def _derive_traces() -> list[dict]:
    """Build per-trace summaries from the spans table.

    Returns a list of dicts, one per trace with >= 2 tool spans.
    """
    with connect() as c:
        rows = c.execute(
            """
            SELECT trace_id, span_kind, tool_name, classifier_task_class,
                   start_time, llm_cost_usd, tool_cost_usd
            FROM spans
            ORDER BY trace_id, start_time
            """
        ).fetchall()

    by_trace: dict[str, dict] = defaultdict(lambda: {
        "tools": [],
        "task_class": None,
        "llm_cost_usd": 0.0,
        "tool_cost_usd": 0.0,
        "start_time": None,
    })

    for r in rows:
        info = by_trace[r["trace_id"]]
        if info["start_time"] is None:
            info["start_time"] = r["start_time"]
        info["llm_cost_usd"] += r["llm_cost_usd"] or 0.0
        info["tool_cost_usd"] += r["tool_cost_usd"] or 0.0
        if r["span_kind"] == "TOOL" and r["tool_name"]:
            info["tools"].append(r["tool_name"])
            if r["tool_name"] == "task_classifier" and r["classifier_task_class"]:
                info["task_class"] = r["classifier_task_class"]

    out = []
    for trace_id, info in by_trace.items():
        out.append({
            "trace_id": trace_id,
            "task_class": info["task_class"] or "unknown",
            "tools": info["tools"],
            "start_time": info["start_time"],
            "llm_cost_usd": info["llm_cost_usd"],
            "tool_cost_usd": info["tool_cost_usd"],
            "total_cost_usd": info["llm_cost_usd"] + info["tool_cost_usd"],
        })
    return out


def _aggregate_by_class(traces: list[dict]) -> dict:
    by_class: dict[str, list] = defaultdict(list)
    for t in traces:
        by_class[t["task_class"]].append(t)

    summary = {}
    for tc, items in by_class.items():
        n = len(items)
        avg_tools = sum(len(t["tools"]) for t in items) / n
        ws_counts = [sum(1 for x in t["tools"] if x == "web_search") for t in items]
        avg_ws = sum(ws_counts) / n
        ws_3_plus = sum(1 for c in ws_counts if c >= 3)
        # Average occurrences of each tool per trace — the savings model
        # needs this to price the calls an optimization would remove.
        tool_totals: Counter = Counter()
        for t in items:
            tool_totals.update(t["tools"])
        avg_tool_counts = {tool: round(cnt / n, 3) for tool, cnt in tool_totals.items()}
        summary[tc] = {
            "n": n,
            "avg_tools": round(avg_tools, 2),
            "avg_web_search": round(avg_ws, 2),
            "traces_with_3plus_web_search": ws_3_plus,
            "avg_cost_usd": round(sum(t["total_cost_usd"] for t in items) / n, 5),
            "avg_llm_cost_usd": round(sum(t["llm_cost_usd"] for t in items) / n, 5),
            "avg_tool_cost_usd": round(sum(t["tool_cost_usd"] for t in items) / n, 5),
            "avg_tool_counts": avg_tool_counts,
        }
    return summary


def _window_days(traces: list[dict]) -> float:
    """Observed time span of the trace set, in days. Used to project a
    monthly rate. Floors at a fraction of a day so a tight burst doesn't
    explode the extrapolation."""
    times = []
    for t in traces:
        ts = t.get("start_time")
        if not ts:
            continue
        try:
            times.append(datetime.fromisoformat(ts))
        except (TypeError, ValueError):
            continue
    if len(times) < 2:
        return 1.0
    span = (max(times) - min(times)).total_seconds() / 86400.0
    return max(span, 0.5)


def _detect_anomalies(traces: list[dict], by_class: dict) -> list[dict]:
    anomalies: list[dict] = []
    baseline = by_class.get(BASELINE_CLASS)
    baseline_cost = (baseline or {}).get("avg_cost_usd") or 1e-9

    if baseline:
        for tc, summary in by_class.items():
            if tc in (BASELINE_CLASS, "unknown"):
                continue
            uplift = summary["avg_cost_usd"] / baseline_cost
            if uplift >= UPLIFT_THRESHOLD_X:
                anomalies.append({
                    "type": "class_cost_uplift",
                    "task_class": tc,
                    "baseline_class": BASELINE_CLASS,
                    "uplift_x": round(uplift, 2),
                    "avg_cost_usd": summary["avg_cost_usd"],
                    "baseline_cost_usd": baseline["avg_cost_usd"],
                    "n_traces": summary["n"],
                })

    by_class_traces: dict[str, list] = defaultdict(list)
    for t in traces:
        by_class_traces[t["task_class"]].append(t)

    for tc, items in by_class_traces.items():
        if tc == "unknown" or not items:
            continue
        repeat_hits: Counter = Counter()
        for t in items:
            counts = Counter(t["tools"])
            for tool_name, c in counts.items():
                if c >= REPEAT_THRESHOLD and tool_name != "task_classifier":
                    repeat_hits[tool_name] += 1
        for tool_name, hits in repeat_hits.items():
            rate = hits / len(items)
            if rate >= REPEAT_HIT_RATE:
                anomalies.append({
                    "type": "repeated_tool",
                    "task_class": tc,
                    "tool": tool_name,
                    "repeat_threshold": REPEAT_THRESHOLD,
                    "traces_with_repeat": hits,
                    "of_total_in_class": len(items),
                    "hit_rate": round(rate, 3),
                })

    return anomalies


def run_detection() -> dict:
    """Compute the current state vector — aggregates + anomalies.

    Cheap enough to call on every ingest batch. Returns a state dict
    with: now (ISO timestamp), by_task_class, anomalies, total_traces,
    total_cost_usd.
    """
    traces = _derive_traces()
    by_class = _aggregate_by_class(traces)
    anomalies = _detect_anomalies(traces, by_class)
    window_days = _window_days(traces)
    # Group raw anomalies into one actionable issue per task class, with
    # savings projected over the observed window. Imported here to keep
    # the dependency one-way (savings -> pricing only).
    from accountant.analytics.savings import build_issues
    issues = build_issues(by_class, anomalies, window_days)
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "total_traces": len(traces),
        "total_cost_usd": round(sum(t["total_cost_usd"] for t in traces), 4),
        "window_days": round(window_days, 2),
        "by_task_class": by_class,
        "anomalies": anomalies,
        "issues": issues,
    }


def anomaly_signature(a: dict) -> str:
    """Stable signature for an anomaly — used as the recommendations PK."""
    if a["type"] == "class_cost_uplift":
        return f"class_cost_uplift:{a['task_class']}"
    if a["type"] == "repeated_tool":
        return f"repeated_tool:{a['task_class']}:{a['tool']}"
    return f"unknown:{json.dumps(a, sort_keys=True)}"
