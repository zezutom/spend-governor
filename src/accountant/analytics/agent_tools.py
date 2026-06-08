"""Agent-facing tools for the Accountant.

These wrap analysis.py and surface compact, structured summaries that
fit comfortably in Gemini's context. Raw bulk-trace operations are
intentionally not exposed; Gemini drills into individual traces via
the Phoenix MCP `get-trace` tool when it needs more detail.
"""

import json
import os
from datetime import timedelta

from accountant.analytics.analysis import (
    aggregate_by_task_class,
    fetch_traces,
    find_anomalies,
)


DEFAULT_PROJECT = os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")


def summarize_project_cost(hours_back: int = 2) -> dict:
    """Pull recent traces and return cost summary by task class.

    Args:
        hours_back: How many hours back from now to look for traces.
                    Default 2.

    Returns a dict with:
      - project: project name
      - window_hours: hours_back as queried
      - total_traces: total number of complete traces (>=2 tool calls)
      - total_cost_usd: sum across all traces
      - by_task_class: mapping of task_class -> {n, avg_tools,
        avg_web_search, traces_with_3plus_web_search, avg_cost_usd,
        avg_llm_cost_usd, avg_tool_cost_usd}
    """
    traces = fetch_traces(DEFAULT_PROJECT, since=timedelta(hours=hours_back))
    by_class = aggregate_by_task_class(traces)
    total_cost = sum(t["cost"]["total_usd"] for t in traces)
    return {
        "project": DEFAULT_PROJECT,
        "window_hours": hours_back,
        "total_traces": len(traces),
        "total_cost_usd": round(total_cost, 4),
        "by_task_class": by_class,
    }


def find_cost_anomalies(hours_back: int = 2) -> dict:
    """Pull recent traces and return detected cost anomalies.

    Args:
        hours_back: How many hours back from now to look for traces.
                    Default 2.

    Returns a dict with:
      - project: project name
      - window_hours: hours_back as queried
      - total_traces: total complete traces analyzed
      - anomalies: list of anomaly dicts. Two anomaly types may appear:
        * type=class_cost_uplift: a task_class costs >=1.5x the
          password_reset baseline. Fields: task_class, uplift_x,
          avg_cost_usd, baseline_cost_usd, n_traces.
        * type=repeated_tool: a tool fires >=3 times within a single
          trace in at least 10% of a task_class's traces. Fields:
          task_class, tool, repeat_threshold, traces_with_repeat,
          of_total_in_class, hit_rate.
        Each anomaly also carries example_trace_ids: real trace ids you
        can pass straight to get-trace to inspect the raw spans.
    """
    traces = fetch_traces(DEFAULT_PROJECT, since=timedelta(hours=hours_back))
    by_class = aggregate_by_task_class(traces)
    anomalies = find_anomalies(traces, by_class)
    return {
        "project": DEFAULT_PROJECT,
        "window_hours": hours_back,
        "total_traces": len(traces),
        "anomalies": anomalies,
    }


def write_report(path: str, content: dict) -> dict:
    """Write the Accountant's findings to a JSON file on disk.

    Args:
        path: destination path (e.g. "examples/accountant-report.json").
              Relative paths resolve against the project root.
        content: JSON-serializable dict — typically containing summary,
                 anomalies, and recommendations.

    Returns {"status": "ok", "path": <absolute_path>, "bytes": <size>}.
    """
    abs_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    payload = json.dumps(content, indent=2)
    with open(abs_path, "w") as f:
        f.write(payload)
    return {"status": "ok", "path": abs_path, "bytes": len(payload)}
