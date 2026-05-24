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

from accountant.db import connect


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
        summary[tc] = {
            "n": n,
            "avg_tools": round(avg_tools, 2),
            "avg_web_search": round(avg_ws, 2),
            "traces_with_3plus_web_search": ws_3_plus,
            "avg_cost_usd": round(sum(t["total_cost_usd"] for t in items) / n, 5),
            "avg_llm_cost_usd": round(sum(t["llm_cost_usd"] for t in items) / n, 5),
            "avg_tool_cost_usd": round(sum(t["tool_cost_usd"] for t in items) / n, 5),
        }
    return summary


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
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "total_traces": len(traces),
        "total_cost_usd": round(sum(t["total_cost_usd"] for t in traces), 4),
        "by_task_class": by_class,
        "anomalies": anomalies,
    }


def anomaly_signature(a: dict) -> str:
    """Stable signature for an anomaly — used as the recommendations PK."""
    if a["type"] == "class_cost_uplift":
        return f"class_cost_uplift:{a['task_class']}"
    if a["type"] == "repeated_tool":
        return f"repeated_tool:{a['task_class']}:{a['tool']}"
    return f"unknown:{json.dumps(a, sort_keys=True)}"
