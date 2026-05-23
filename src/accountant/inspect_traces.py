"""Inspect recent traces in Phoenix to verify the observed agent's behavior.

Pulls spans for the current Phoenix project, groups by trace_id, and
reports the tool sequence, task classification, and per-trace cost
breakdown for each trace, plus aggregates by task_class. The headline
signals are (a) the % of refund traces with >= 3 web_search calls and
(b) the avg cost gap between refund_handling and the other task types
— the anti-pattern, in dollars.

Run with:
    uv run python -m accountant.inspect_traces [--since 1h] [--show N] [--limit N]

--since accepts a relative window: '30m', '4h', '7d'. Without it, all
spans returned (up to --limit) are included; this is useful for a broad
sweep but pulls in older traces too. Use --since to focus on recent
runs.
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

if "PHOENIX_API_KEY_OBSERVED_WRITE" in os.environ:
    os.environ.setdefault("PHOENIX_API_KEY", os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"])

PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")

from phoenix.client import Client

from accountant.cost import compute_trace_cost
from accountant.pricing.gemini import MODELS
from accountant.pricing.tools import TOOL_PRICES


DEFAULT_MODEL = "gemini-2.5-flash"


def _attr(row, *keys):
    """Return the first non-null value among the candidate column names."""
    for k in keys:
        if k in row:
            value = row[k]
            if value is None:
                continue
            try:
                if value != value:  # NaN
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
    # ADK wraps tool returns as {"id": ..., "name": ..., "response": {...}}.
    response = data.get("response", data)
    if isinstance(response, dict):
        return response.get("task_class")
    return None


def _parse_since(value: str) -> datetime:
    """Parse '30m', '4h', '7d' into a UTC datetime."""
    unit = value[-1].lower()
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        raise ValueError(f"--since unit must be m/h/d, got '{value}'")
    n = int(value[:-1])
    return datetime.now(timezone.utc) - timedelta(seconds=n * multipliers[unit])


def _gemini_usage_from_span(span) -> dict:
    """Reshape OpenInference LLM token attributes into Gemini usage_metadata."""
    return {
        "prompt_token_count": int(_attr(span, "attributes.llm.token_count.prompt") or 0),
        "cached_content_token_count": int(_attr(
            span,
            "attributes.llm.token_count.prompt_details.cache_read",
            "attributes.llm.token_count.cache_read",
        ) or 0),
        "candidates_token_count": int(_attr(span, "attributes.llm.token_count.completion") or 0),
        "thoughts_token_count": int(_attr(
            span,
            "attributes.llm.token_count.completion_details.reasoning",
        ) or 0),
    }


def main(show_n: int, span_limit: int, since: datetime | None) -> None:
    client = Client()
    if since is None:
        df = client.spans.get_spans_dataframe(
            project_identifier=PROJECT_NAME,
            limit=span_limit,
            timeout=300,
        )
    else:
        # Phoenix Cloud disconnects on large single-response pulls, so
        # chunk the window into 10-minute slices. Walk newest → oldest
        # so a mid-pull failure still leaves us with the most recent
        # traces (the ones we care about) rather than the oldest.
        chunks: list[pd.DataFrame] = []
        end = datetime.now(timezone.utc)
        chunk = timedelta(minutes=10)
        cursor = end
        while cursor > since:
            prev = max(cursor - chunk, since)
            df_chunk = client.spans.get_spans_dataframe(
                project_identifier=PROJECT_NAME,
                limit=span_limit,
                start_time=prev,
                end_time=cursor,
                timeout=300,
            )
            if not df_chunk.empty:
                chunks.append(df_chunk)
            cursor = prev
        df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if df.empty:
        print("No spans returned.")
        return

    df = df.sort_values("start_time")

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
                # Strip provider prefixes like "gemini/gemini-2.5-flash".
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

    # Compute cost per trace.
    for info in traces.values():
        info["cost"] = compute_trace_cost(
            llm_usages=info["llm_usages"],
            tool_calls=info["tools"],
            model_prices=MODELS,
            tool_prices=TOOL_PRICES,
        )

    # Drop short/partial traces: anything with fewer than 2 tool calls
    # is either an errored run or an ad-hoc test interaction, not a
    # complete ticket. Keeps the aggregates honest.
    full_traces = [(tid, info) for tid, info in traces.items() if len(info["tools"]) >= 2]
    dropped = len(traces) - len(full_traces)

    ordered = sorted(
        full_traces,
        key=lambda kv: kv[1]["start_time"] or 0,
        reverse=True,
    )

    window = f" since {since.isoformat(timespec='minutes')}" if since else ""
    suffix = f" (dropped {dropped} partial)" if dropped else ""
    print(f"Loaded {len(df)} spans across {len(ordered)} traces{window}{suffix}.\n")

    print(f"Most recent {min(show_n, len(ordered))} traces:")
    print("-" * 110)
    for trace_id, info in ordered[:show_n]:
        tc = info["task_class"] or "?"
        ws = sum(1 for t in info["tools"] if t == "web_search")
        cost = info["cost"]["total_usd"]
        print(
            f"  [{tc:18s}] ${cost:7.5f}  ws={ws}  "
            f"tools={info['tools']}  trace={trace_id[:16]}"
        )

    print("\nAggregates by task_class:")
    print("-" * 110)
    by_class: dict[str, list] = defaultdict(list)
    for _, info in ordered:
        by_class[info["task_class"] or "?"].append(info)

    for tc, items in sorted(by_class.items()):
        n = len(items)
        avg_tools = sum(len(i["tools"]) for i in items) / n
        ws_counts = [sum(1 for t in i["tools"] if t == "web_search") for i in items]
        avg_ws = sum(ws_counts) / n
        ws_3_plus = sum(1 for c in ws_counts if c >= 3)
        pct = 100 * ws_3_plus / n
        avg_cost = sum(i["cost"]["total_usd"] for i in items) / n
        avg_llm = sum(i["cost"]["llm_total_usd"] for i in items) / n
        avg_tool_cost = sum(i["cost"]["tool_total_usd"] for i in items) / n
        print(
            f"  {tc:18s}  n={n:4d}  avg_tools={avg_tools:5.2f}  "
            f"avg_ws={avg_ws:4.2f}  >=3_ws={ws_3_plus}/{n} ({pct:3.0f}%)  "
            f"avg_cost=${avg_cost:.5f}  (llm=${avg_llm:.5f}  tool=${avg_tool_cost:.5f})"
        )


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--show", type=int, default=20,
                   help="number of most-recent traces to list (default 20)")
    p.add_argument("--limit", type=int, default=2000,
                   help="max spans to pull from Phoenix (default 2000)")
    p.add_argument("--since", type=str, default=None,
                   help="filter to spans newer than this window, e.g. '30m', '4h', '7d'")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    since = _parse_since(args.since) if args.since else None
    main(args.show, args.limit, since)
