"""One-shot Phoenix backfill for onboarding.

When the Governor ingest server starts and finds an empty spans cache
(a first-time / new-tenant install), this module pulls historical spans
from Phoenix and writes them into the local SQLite cache so the
operator sees meaningful state immediately rather than an empty
dashboard.

Runs asynchronously in the background — server start is not blocked.
Progress is written to state_meta.backfill_state as a JSON blob; the
dashboard reads that blob and renders a progress banner.

The pull strategy mirrors the live ingest pipeline: 10-minute chunks
walked newest → oldest so the most recent activity appears first.
Each chunk is converted to the same normalized span shape the live
exporter sends, then routed through the same `_row_for_span` helper
that the worker uses, so cost-computation logic stays in one place.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from phoenix.client import Client

from governor.analytics import reasoning
from governor.pipeline.db import connect, set_meta, upsert_span
from governor.analytics.detection import (
    _aggregate_by_class,
    _detect_anomalies,
    run_detection,
)
from governor.analytics.recommendations import generate_templated_recommendations
from governor.pipeline.worker import _row_for_span, reconcile_from_phoenix


log = logging.getLogger(__name__)


# If GOVERNOR_BACKFILL_HOURS is set, use a fixed window. Otherwise the
# default is "exhaustive": walk backwards in time until we see
# CONSECUTIVE_EMPTY_STOP chunks in a row (end of project history) or
# hit the HARD_FLOOR_DAYS safety limit.
BACKFILL_HOURS_ENV = os.environ.get("GOVERNOR_BACKFILL_HOURS")
CHUNK_MINUTES = 10
SPAN_LIMIT_PER_CHUNK = 5000
# 250 × 10-min chunks = ~42 hours of empty before we conclude the
# project history has truly ended. Generous enough to bridge overnight
# idle periods or a weekend; tight enough that fresh tenants don't
# wait through 90 days of empties.
CONSECUTIVE_EMPTY_STOP = int(os.environ.get("GOVERNOR_BACKFILL_EMPTY_STOP", "250"))
HARD_FLOOR_DAYS = int(os.environ.get("GOVERNOR_BACKFILL_FLOOR_DAYS", "90"))


# Module-level handle so /backfill/start is idempotent — if a task is
# already running, the endpoint just returns its status.
_active_task: asyncio.Task | None = None


def cache_is_empty() -> bool:
    with connect() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM spans").fetchone()
        return (row["n"] or 0) == 0


def _phoenix_client() -> Client:
    if "PHOENIX_API_KEY_OBSERVED_WRITE" in os.environ:
        os.environ.setdefault("PHOENIX_API_KEY", os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"])
    return Client()


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


def _ts_to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, pd.Timestamp):
        return value.tz_convert("UTC").isoformat() if value.tzinfo else value.tz_localize("UTC").isoformat()
    try:
        return pd.Timestamp(value).tz_convert("UTC").isoformat()
    except Exception:
        return str(value)


def _phoenix_row_to_normalized(row) -> dict | None:
    """Map a Phoenix DataFrame row to the same JSON shape the live OTel
    exporter sends. Returns None if essential fields are missing."""
    trace_id = _attr(row, "context.trace_id", "trace_id")
    span_id = _attr(row, "context.span_id", "span_id")
    start_time = _ts_to_iso(_attr(row, "start_time"))
    if not (trace_id and span_id and start_time):
        return None

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": _attr(row, "parent_id"),
        "name": _attr(row, "name"),
        "start_time": start_time,
        "end_time": _ts_to_iso(_attr(row, "end_time")),
        "openinference_kind": _attr(
            row,
            "attributes.openinference.span.kind",
            "openinference.span.kind",
            "span_kind",
        ),
        "tool_name": _attr(row, "attributes.tool.name", "tool.name"),
        "output_value": _attr(row, "attributes.output.value", "output.value"),
        "cache_hit": bool(_attr(row, "attributes.accountant.cache_hit", "accountant.cache_hit")),
        "prompt_tokens": _attr(row, "attributes.llm.token_count.prompt"),
        "cached_input_tokens": _attr(
            row,
            "attributes.llm.token_count.prompt_details.cache_read",
            "attributes.llm.token_count.cache_read",
        ),
        "completion_tokens": _attr(row, "attributes.llm.token_count.completion"),
        "reasoning_tokens": _attr(
            row, "attributes.llm.token_count.completion_details.reasoning"
        ),
        "model_name": _attr(row, "attributes.llm.model_name"),
    }


MINI_BATCH_SIZE = 25


def _ingest_mini_batch(df_mini: pd.DataFrame) -> tuple[int, int, float]:
    """Insert one mini-batch of spans (≤ MINI_BATCH_SIZE) plus return the
    current cache totals. Splitting a chunk into mini-batches lets the
    banner counters climb smoothly during busy chunks instead of
    jumping by 1000+ at chunk boundaries.
    """
    written = 0
    if df_mini is not None and not df_mini.empty:
        with connect() as c:
            c.execute("BEGIN")
            for _, row in df_mini.iterrows():
                normalized = _phoenix_row_to_normalized(row)
                if normalized is None:
                    continue
                span_row = _row_for_span(normalized)
                upsert_span(span_row, conn=c)
                written += 1
            c.execute("COMMIT")
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT trace_id) AS traces, "
            "COALESCE(SUM(llm_cost_usd + tool_cost_usd), 0) AS cost "
            "FROM spans"
        ).fetchone()
        trace_count = int(row["traces"] or 0)
        total_cost = float(row["cost"] or 0)
    return written, trace_count, total_cost


def _current_totals() -> tuple[int, float]:
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT trace_id) AS traces, "
            "COALESCE(SUM(llm_cost_usd + tool_cost_usd), 0) AS cost "
            "FROM spans"
        ).fetchone()
        return int(row["traces"] or 0), float(row["cost"] or 0)


def _update_in_memory(traces: dict, span_row: dict) -> None:
    """Update the in-memory trace summary for one span. Used to drive
    the unified live_state snapshot without re-scanning the DB."""
    tid = span_row["trace_id"]
    info = traces.setdefault(tid, {
        "tools": [],
        "task_class": None,
        "start_time": span_row.get("start_time"),
        "llm_cost_usd": 0.0,
        "tool_cost_usd": 0.0,
        "n_spans": 0,
    })
    info["n_spans"] += 1
    if info["start_time"] is None:
        info["start_time"] = span_row.get("start_time")
    if span_row.get("span_kind") == "TOOL" and span_row.get("tool_name"):
        info["tools"].append(span_row["tool_name"])
        tc = span_row.get("classifier_task_class")
        if span_row["tool_name"] == "task_classifier" and tc:
            info["task_class"] = tc
    info["llm_cost_usd"] += span_row.get("llm_cost_usd") or 0.0
    info["tool_cost_usd"] += span_row.get("tool_cost_usd") or 0.0


def _write_live_state(traces_in_memory: dict, ingest: dict) -> dict:
    """Compute the unified live_state snapshot from in-memory state and
    write to state_meta. Returns the snapshot dict for reuse.

    This is the ONE writer for live state — every UI element on the
    dashboard reads from this single blob, eliminating two-cadence
    divergence between banner counters and header counters.
    """
    trace_list = []
    total_llm = 0.0
    total_tool = 0.0
    total_spans = 0
    for tid, info in traces_in_memory.items():
        llm_cost = info["llm_cost_usd"]
        tool_cost = info["tool_cost_usd"]
        trace_list.append({
            "trace_id": tid,
            "task_class": info["task_class"] or "unknown",
            "tools": list(info["tools"]),
            "start_time": info["start_time"],
            "llm_cost_usd": llm_cost,
            "tool_cost_usd": tool_cost,
            "total_cost_usd": llm_cost + tool_cost,
        })
        total_llm += llm_cost
        total_tool += tool_cost
        total_spans += info["n_spans"]

    by_class = _aggregate_by_class(trace_list) if trace_list else {}
    anomalies = _detect_anomalies(trace_list, by_class) if by_class else []

    snap = {
        "ingest": dict(ingest),
        "summary": {
            "total_traces": len(trace_list),
            "total_spans": total_spans,
            "total_llm_cost_usd": round(total_llm, 6),
            "total_tool_cost_usd": round(total_tool, 6),
            "total_cost_usd": round(total_llm + total_tool, 6),
            "last_updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "by_task_class": by_class,
        "anomalies": anomalies,
    }
    set_meta("live_state", json.dumps(snap))
    return snap


def _ingest_per_span(
    df: pd.DataFrame,
    ingest: dict,
    prev,
    cursor,
    chunk_index: int,
    rows_in_chunk: int,
    traces_in_memory: dict,
) -> None:
    """Insert each span one at a time. After every successful insert,
    compute the unified live_state from in-memory data and write it as
    ONE blob to state_meta. Banner and dashboard sections all read
    from that single blob — no divergent cadences."""
    inserted_in_chunk = 0
    prev_iso = prev.isoformat()
    chunk_lookback = _humanize_lookback(prev)

    with connect() as c:
        for _, row in df.iterrows():
            normalized = _phoenix_row_to_normalized(row)
            if normalized is None:
                continue
            span_row = _row_for_span(normalized)

            c.execute("BEGIN")
            upsert_span(span_row, conn=c)
            c.execute("COMMIT")

            _update_in_memory(traces_in_memory, span_row)
            inserted_in_chunk += 1

            ingest["chunks_processed"] = chunk_index
            ingest["cursor_iso"] = prev_iso
            ingest["lookback_human"] = chunk_lookback
            ingest["message"] = (
                f"Importing activity from {chunk_lookback} "
                f"({inserted_in_chunk}/{rows_in_chunk} in this batch)"
            )
            _write_live_state(traces_in_memory, ingest)


async def _estimate_total_traces(project: str) -> int | None:
    """One-shot quick estimate of total traces in the project, used as
    the progress-bar denominator. Best-effort — returns None if it
    can't be computed quickly.

    Strategy: hit the Phoenix REST traces endpoint directly with a
    large limit; the response is a flat array, so its length is the
    total (or at least a lower bound paginated by next_cursor).
    """
    api_key = os.environ.get("PHOENIX_API_KEY") or os.environ.get(
        "PHOENIX_API_KEY_OBSERVED_WRITE"
    )
    base = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    if not api_key or not base:
        return None

    import httpx

    total = 0
    cursor = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(50):
                params: dict = {"limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                url = f"{base.rstrip('/')}/v1/projects/{project}/traces"
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    params=params,
                )
                if resp.status_code != 200:
                    return total or None
                payload = resp.json()
                items = payload.get("data") or payload if isinstance(payload, list) else []
                if isinstance(payload, dict):
                    items = payload.get("data", [])
                total += len(items)
                cursor = (payload or {}).get("next_cursor") if isinstance(payload, dict) else None
                if not cursor:
                    break
        return total
    except Exception:
        log.exception("trace count estimate failed")
        return None


def _project_name() -> str:
    return os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")


def _humanize_lookback(cursor: datetime) -> str:
    """Convert a UTC timestamp into a human-readable 'how long ago' phrase.
    The user does not care about UTC slice boundaries."""
    delta = datetime.now(timezone.utc) - cursor
    seconds = delta.total_seconds()
    if seconds < 600:
        return "the last few minutes"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    if seconds < 7200:
        return "the last hour"
    if seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    if seconds < 86400 * 2:
        return "yesterday"
    if seconds < 86400 * 7:
        return f"{int(seconds / 86400)} days ago"
    if seconds < 86400 * 30:
        weeks = int(seconds / 86400 / 7)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = int(seconds / 86400 / 30)
    return f"{months} month{'s' if months != 1 else ''} ago"


async def run_backfill(hours: int | None = None) -> None:
    """Walk Phoenix newest→oldest in 10-min chunks, write to SQLite,
    update progress + detection after each chunk.

    If hours is None (default), runs exhaustively: keeps walking back
    until CONSECUTIVE_EMPTY_STOP empty chunks in a row, or until the
    HARD_FLOOR_DAYS safety limit. This is what we want for new-tenant
    onboarding — pull everything the project has.

    If hours is an int, runs with a fixed time window (legacy behavior;
    useful for testing).
    """
    project = _project_name()
    chunk = timedelta(minutes=CHUNK_MINUTES)
    end = datetime.now(timezone.utc)

    if hours is not None:
        floor = end - timedelta(hours=hours)
        mode = "windowed"
        total_chunks_est = max(1, int(timedelta(hours=hours) / chunk))
    else:
        floor = end - timedelta(days=HARD_FLOOR_DAYS)
        mode = "exhaustive"
        total_chunks_est = None

    # The single in-memory store. Both the headline counters and the
    # by-class aggregates derive from this dict, so the dashboard
    # never sees a divergent snapshot between counters.
    traces_in_memory: dict = {}

    # Ingest metadata — backfill progress, cursor, status. Lives inside
    # live_state under the "ingest" key.
    ingest: dict = {
        "status": "in_progress",
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "processed_chunks": 0,
        "consecutive_empty": 0,
        "cursor_iso": end.isoformat(),
        "message": f"Connecting to Phoenix project '{project}'…",
        "project": project,
    }
    if total_chunks_est is not None:
        ingest["total_chunks"] = total_chunks_est
        ingest["progress"] = 0.0
    _write_live_state(traces_in_memory, ingest)
    log.info("backfill starting: mode=%s", mode)

    # One-shot estimate of total traces so the banner can show "X / ~Y".
    ingest["message"] = "Estimating total traces in Phoenix…"
    _write_live_state(traces_in_memory, ingest)
    estimated_total = await _estimate_total_traces(project)
    if estimated_total is not None:
        ingest["estimated_total_traces"] = estimated_total
        log.info("trace estimate: ~%s", estimated_total)
    _write_live_state(traces_in_memory, ingest)

    client = _phoenix_client()
    cursor = end
    processed = 0
    consecutive_empty = 0

    try:
        while cursor > floor:
            prev = max(cursor - chunk, floor)
            await asyncio.sleep(0)

            try:
                df = await asyncio.to_thread(
                    client.spans.get_spans_dataframe,
                    project_identifier=project,
                    limit=SPAN_LIMIT_PER_CHUNK,
                    start_time=prev,
                    end_time=cursor,
                    timeout=300,
                )
            except Exception as e:
                log.warning("phoenix fetch chunk %s-%s failed: %s", prev, cursor, e)
                df = pd.DataFrame()

            rows = 0 if (df is None or df.empty) else len(df)

            if rows > 0:
                consecutive_empty = 0
                await asyncio.to_thread(
                    _ingest_per_span,
                    df,
                    ingest,
                    prev,
                    cursor,
                    processed + 1,
                    rows,
                    traces_in_memory,
                )
            else:
                consecutive_empty += 1

            processed += 1
            ingest["processed_chunks"] = processed
            ingest["consecutive_empty"] = consecutive_empty
            ingest["cursor_iso"] = prev.isoformat()

            lookback = _humanize_lookback(prev)
            ingest["lookback_human"] = lookback
            if rows > 0:
                ingest["message"] = f"Found activity from {lookback}"
            else:
                ingest["message"] = f"Searching activity from {lookback}…"
            if total_chunks_est is not None:
                ingest["progress"] = round(min(processed / total_chunks_est, 1.0), 3)
            _write_live_state(traces_in_memory, ingest)

            # Full detection at chunk boundaries — by-class table and
            # recommendation cards refresh per chunk (mini-batch
            # Early-exit: if we've imported every trace Phoenix told us
            # exists (via the upfront estimate), we're done — no need
            # to walk through 42 more hours of empty space to confirm.
            est = ingest.get("estimated_total_traces")
            if est and len(traces_in_memory) >= est:
                ingest["message"] = (
                    f"All {est:,} traces imported — analysis ready."
                )
                _write_live_state(traces_in_memory, ingest)
                break

            if mode == "exhaustive" and consecutive_empty >= CONSECUTIVE_EMPTY_STOP:
                ingest["message"] = (
                    "Reached the end of your project's history."
                )
                _write_live_state(traces_in_memory, ingest)
                break

            cursor = prev

        total_spans = sum(t["n_spans"] for t in traces_in_memory.values())
        total_traces = len(traces_in_memory)
        ingest.update({
            "status": "complete",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "progress": 1.0,
            "message": (
                f"Backfill complete: {total_spans:,} spans, "
                f"{total_traces:,} traces, {processed} chunks."
            ),
        })
        _write_live_state(traces_in_memory, ingest)
        # Refactor #2: backfilled spans are historical, so Phoenix has
        # already computed their cost — reconcile now to source actual LLM
        # cost + savings from Phoenix before detection runs over the cache.
        try:
            await asyncio.to_thread(reconcile_from_phoenix, 50000)
        except Exception:
            log.exception("post-backfill Phoenix reconcile failed")
        # Run detection from the DB to compute deduped, costed issues
        # (build_issues needs the aggregates + window), then generate
        # templated recommendations and kick off Gemini reasoning.
        det = await asyncio.to_thread(run_detection)
        await asyncio.to_thread(generate_templated_recommendations, det)
        reasoning.schedule_if_changed(det.get("issues", []))
        log.info(
            "backfill complete: %s spans, %s traces, %s chunks",
            total_spans, total_traces, processed,
        )

    except asyncio.CancelledError:
        ingest["status"] = "cancelled"
        ingest["message"] = "Backfill cancelled."
        _write_live_state(traces_in_memory, ingest)
        raise
    except Exception as e:
        log.exception("backfill failed")
        ingest["status"] = "failed"
        ingest["message"] = f"Backfill failed: {e}"
        ingest["error"] = str(e)
        _write_live_state(traces_in_memory, ingest)


def start_backfill_if_idle() -> dict:
    """Start the onboarding backfill if no task is running AND the cache
    is empty. Idempotent — safe to call repeatedly (e.g. from the
    dashboard on each rerun).

    The empty-cache guard means a normal restart with synced data never
    re-imports: onboarding is a new-account, empty-cache operation only.
    To force a fresh import, delete data/accountant.db first.
    """
    global _active_task

    if _active_task is not None and not _active_task.done():
        return {"status": "already_running"}

    if not cache_is_empty():
        return {"status": "skipped", "reason": "cache already populated"}

    hours = int(BACKFILL_HOURS_ENV) if BACKFILL_HOURS_ENV else None
    _active_task = asyncio.create_task(run_backfill(hours=hours))
    return {"status": "started", "mode": "windowed" if hours else "exhaustive"}


def maybe_start_backfill() -> asyncio.Task | None:
    """Deprecated auto-trigger entry point — kept for backward compat
    but the lifespan no longer calls it. Onboarding now triggers via
    /backfill/start from the dashboard.
    """
    return None
