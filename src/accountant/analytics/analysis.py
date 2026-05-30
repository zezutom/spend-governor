"""Trace aggregation and anomaly detection.

Pulls spans from Phoenix via the Python SDK, groups them into traces,
computes per-trace cost using cost.py, and produces:

- by-task-class aggregates (n, avg cost, avg tools, web_search counts)
- anomalies (statistical: top-cost-within-class; pattern: repeated tool
  calls within a single trace)

The aggregation logic is structurally similar to inspect_traces.py but
returns plain dicts so it can be called from agent tools as well as
from CLI.
"""

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
from phoenix.client import Client

from accountant.pricing.cost import compute_trace_cost
from accountant.pricing.gemini import MODELS
from accountant.pricing.tools import TOOL_PRICES


DEFAULT_MODEL = "gemini-2.5-flash"


def _attr(row, *keys):
    for k in keys:
        if k in row:
            value = row[k]
            if value is None:
                continue
            try:
                if value != value:
                    continue
            except TypeError:
                pass
            if value == "":
                continue
            return value
    return None


def _parse_classifier_output(raw):
    if raw is None:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    response = data.get("response", data)
    if isinstance(response, dict):
        return response.get("task_class")
    return None


def _gemini_usage_from_span(span) -> dict:
    # The OpenInference instrumentor sets llm.token_count.completion to
    # candidates + thoughts, and reports reasoning as a SUBSET of it via
    # completion_details.reasoning. token_usage_from_gemini follows raw
    # Gemini semantics (candidates EXCLUDES thoughts, then adds them back),
    # so feed it candidates = completion - reasoning; otherwise reasoning
    # tokens are counted twice.
    completion = int(_attr(span, "attributes.llm.token_count.completion") or 0)
    reasoning = int(_attr(
        span,
        "attributes.llm.token_count.completion_details.reasoning",
    ) or 0)
    return {
        "prompt_token_count": int(_attr(span, "attributes.llm.token_count.prompt") or 0),
        "cached_content_token_count": int(_attr(
            span,
            "attributes.llm.token_count.prompt_details.cache_read",
            "attributes.llm.token_count.cache_read",
        ) or 0),
        "candidates_token_count": max(completion - reasoning, 0),
        "thoughts_token_count": reasoning,
    }


def _phoenix_client() -> Client:
    if "PHOENIX_API_KEY_OBSERVED_WRITE" in os.environ:
        os.environ.setdefault("PHOENIX_API_KEY", os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"])
    return Client()


def _auto_chunk_size(since: timedelta) -> timedelta:
    """Always 10 minutes.

    Phoenix Cloud reliably returns 10-min chunks even when the busy
    minute contains thousands of spans. Larger chunks (30m, 1h) hang
    when they overlap a high-traffic window. Empty chunks are cheap
    (~200ms), so the wide-window overhead of many requests is small.
    """
    return timedelta(minutes=10)


def fetch_traces(
    project: str,
    since: timedelta = timedelta(hours=2),
    chunk: timedelta | None = None,
    span_limit_per_chunk: int = 5000,
) -> list[dict]:
    """Return one dict per trace, walking newest → oldest in time chunks.

    Each trace dict has: trace_id, start_time, task_class, tools (list of
    tool names in invocation order), llm_usages (list of (usage_metadata,
    model_name) pairs), cost (full breakdown from compute_trace_cost).
    """
    client = _phoenix_client()
    end = datetime.now(timezone.utc)
    start = end - since
    if chunk is None:
        chunk = _auto_chunk_size(since)

    total_chunks = max(1, int(since / chunk))
    print(
        f"[analysis] fetching spans from {project}: "
        f"{since} window, {chunk} chunks (~{total_chunks} requests)",
        flush=True,
    )

    chunks: list[pd.DataFrame] = []
    cursor = end
    chunk_idx = 0
    while cursor > start:
        prev = max(cursor - chunk, start)
        chunk_idx += 1
        df_chunk = client.spans.get_spans_dataframe(
            project_identifier=project,
            limit=span_limit_per_chunk,
            start_time=prev,
            end_time=cursor,
            timeout=300,
        )
        rows = 0 if df_chunk.empty else len(df_chunk)
        print(
            f"[analysis] chunk {chunk_idx}/{total_chunks} "
            f"[{prev.strftime('%H:%M')}-{cursor.strftime('%H:%M')}]: {rows} spans",
            flush=True,
        )
        if not df_chunk.empty:
            chunks.append(df_chunk)
        cursor = prev

    print(f"[analysis] fetch complete: {sum(len(c) for c in chunks)} spans total", flush=True)

    if not chunks:
        return []

    df = pd.concat(chunks, ignore_index=True).sort_values("start_time")

    traces: dict[str, dict] = defaultdict(lambda: {
        "tools": [],
        "task_class": None,
        "start_time": None,
        "llm_usages": [],
    })

    for _, span in df.iterrows():
        trace_id = _attr(span, "context.trace_id", "trace_id")
        if not trace_id:
            continue
        info = traces[trace_id]
        if info["start_time"] is None:
            info["start_time"] = span.get("start_time")

        kind = _attr(
            span,
            "attributes.openinference.span.kind",
            "openinference.span.kind",
            "span_kind",
        )

        if kind == "LLM":
            usage = _gemini_usage_from_span(span)
            if usage["prompt_token_count"] or usage["candidates_token_count"]:
                model = _attr(span, "attributes.llm.model_name") or DEFAULT_MODEL
                if "/" in model:
                    model = model.split("/", 1)[1]
                if model not in MODELS:
                    model = DEFAULT_MODEL
                info["llm_usages"].append((usage, model))
            continue

        if kind != "TOOL":
            continue

        tool_name = _attr(span, "attributes.tool.name", "tool.name", "name")
        if not tool_name:
            continue
        info["tools"].append(tool_name)

        if tool_name == "task_classifier":
            raw_output = _attr(span, "attributes.output.value", "output.value")
            tc = _parse_classifier_output(raw_output)
            if tc:
                info["task_class"] = tc

    out = []
    for trace_id, info in traces.items():
        if len(info["tools"]) < 2:
            continue
        cost = compute_trace_cost(
            llm_usages=info["llm_usages"],
            tool_calls=info["tools"],
            model_prices=MODELS,
            tool_prices=TOOL_PRICES,
        )
        out.append({
            "trace_id": trace_id,
            "start_time": info["start_time"].isoformat() if info["start_time"] is not None else None,
            "task_class": info["task_class"],
            "tools": info["tools"],
            "cost": cost,
        })
    out.sort(key=lambda t: t["start_time"] or "", reverse=True)
    return out


def aggregate_by_task_class(traces: list[dict]) -> dict:
    """Group traces by task_class and return one summary row per class."""
    by_class: dict[str, list] = defaultdict(list)
    for t in traces:
        by_class[t["task_class"] or "unknown"].append(t)

    summary = {}
    for tc, items in by_class.items():
        n = len(items)
        avg_tools = sum(len(t["tools"]) for t in items) / n
        ws_counts = [sum(1 for x in t["tools"] if x == "web_search") for t in items]
        avg_ws = sum(ws_counts) / n
        ws_3_plus = sum(1 for c in ws_counts if c >= 3)
        avg_cost = sum(t["cost"]["total_usd"] for t in items) / n
        avg_llm = sum(t["cost"]["llm_total_usd"] for t in items) / n
        avg_tool_cost = sum(t["cost"]["tool_total_usd"] for t in items) / n
        summary[tc] = {
            "n": n,
            "avg_tools": round(avg_tools, 2),
            "avg_web_search": round(avg_ws, 2),
            "traces_with_3plus_web_search": ws_3_plus,
            "avg_cost_usd": round(avg_cost, 5),
            "avg_llm_cost_usd": round(avg_llm, 5),
            "avg_tool_cost_usd": round(avg_tool_cost, 5),
        }
    return summary


def find_anomalies(
    traces: list[dict],
    by_class: dict,
    repeat_threshold: int = 3,
    baseline_class: str = "password_reset",
) -> list[dict]:
    """Detect cost anomalies in the trace set.

    Two detectors:
    1. **Class-level cost uplift.** For each task_class, compare its
       avg cost to the baseline_class avg cost. Flag if >=1.5x.
    2. **Repeated tool pattern.** For each task_class, find tools that
       fire >= repeat_threshold times within a single trace in at least
       10% of that class's traces. Flag the pattern.
    """
    anomalies = []

    baseline = by_class.get(baseline_class)
    if baseline:
        baseline_cost = baseline["avg_cost_usd"] or 1e-9
        for tc, summary in by_class.items():
            if tc == baseline_class or tc == "unknown":
                continue
            uplift = summary["avg_cost_usd"] / baseline_cost
            if uplift >= 1.5:
                anomalies.append({
                    "type": "class_cost_uplift",
                    "task_class": tc,
                    "baseline_class": baseline_class,
                    "uplift_x": round(uplift, 2),
                    "avg_cost_usd": summary["avg_cost_usd"],
                    "baseline_cost_usd": baseline["avg_cost_usd"],
                    "n_traces": summary["n"],
                })

    by_class_traces: dict[str, list] = defaultdict(list)
    for t in traces:
        by_class_traces[t["task_class"] or "unknown"].append(t)

    for tc, items in by_class_traces.items():
        if tc == "unknown" or not items:
            continue
        tool_repeat_hits: Counter = Counter()
        for t in items:
            counts = Counter(t["tools"])
            for tool_name, c in counts.items():
                if c >= repeat_threshold and tool_name != "task_classifier":
                    tool_repeat_hits[tool_name] += 1
        for tool_name, hit_count in tool_repeat_hits.items():
            hit_rate = hit_count / len(items)
            if hit_rate >= 0.10:
                anomalies.append({
                    "type": "repeated_tool",
                    "task_class": tc,
                    "tool": tool_name,
                    "repeat_threshold": repeat_threshold,
                    "traces_with_repeat": hit_count,
                    "of_total_in_class": len(items),
                    "hit_rate": round(hit_rate, 3),
                })

    return anomalies
