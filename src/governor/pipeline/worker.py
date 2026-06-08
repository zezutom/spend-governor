"""Outbox-drainer worker.

Consumes pending span_outbox rows FIFO, computes per-span cost, upserts
into the spans table, then refreshes detection + recommendations.

Runs as an asyncio task inside the same process as the FastAPI ingest
server (single-process MVP). Receiver and worker share SQLite via WAL
mode — receiver's transactional INSERT into span_outbox is the
durability boundary; the worker only ACKs (marks 'processed') after the
batch is fully ingested.
"""

import asyncio
import json
import logging
import time

from governor.pricing.cost import (
    TokenUsage,
    compute_baseline_llm_cost,
)
from governor.pipeline import phoenix_cost
from governor.pipeline.db import (
    claim_pending_batches,
    connect,
    get_meta,
    mark_batch_failed,
    mark_batch_processed,
    mark_spans_annotated,
    set_meta,
    unannotated_saving_spans,
    update_phoenix_costs,
    upsert_span,
)
from governor.analytics import reasoning
from governor.analytics.detection import run_detection
from governor.pricing.gemini import MODELS
from governor.pricing.tools import TOOL_PRICES
from governor.analytics.recommendations import generate_templated_recommendations


log = logging.getLogger(__name__)


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


def _normalize_model(name: str | None) -> str:
    if not name:
        return "gemini-2.5-flash"
    if "/" in name:
        name = name.split("/", 1)[1]
    if name not in MODELS:
        return "gemini-2.5-flash"
    return name


def _cost_for_span(raw: dict) -> tuple[float, float]:
    kind = raw.get("openinference_kind")
    llm = 0.0
    tool = 0.0

    if kind == "LLM":
        prompt = int(raw.get("prompt_tokens") or 0)
        cached = int(raw.get("cached_input_tokens") or 0)
        # completion_tokens is the instrumentor's llm.token_count.completion,
        # which ALREADY includes reasoning (candidates + thoughts). Do NOT add
        # reasoning_tokens again — that double-counts thinking tokens.
        completion = int(raw.get("completion_tokens") or 0)
        if prompt or completion:
            model = _normalize_model(raw.get("model_name"))
            usage = TokenUsage(
                uncached_input_tokens=max(prompt - cached, 0),
                cached_input_tokens=cached,
                output_tokens=completion,
            )
            # Instant fallback: local compute so the dashboard shows a cost
            # immediately on ingest. reconcile_from_phoenix (refactor #2)
            # overwrites llm_cost_usd with Phoenix's authoritative cost and
            # flips cost_source to 'phoenix' once Phoenix has costed the
            # span; our compute matches Phoenix to the cent, so the number
            # barely moves on reconcile.
            llm = compute_baseline_llm_cost(usage, MODELS[model])["total_usd"]

    if kind == "TOOL":
        # A wrapper cache hit means the real (paid) call never ran —
        # price it at $0 so the trace-measured cost reflects the saving.
        if raw.get("cache_hit"):
            tool = 0.0
        else:
            tool = TOOL_PRICES.get(raw.get("tool_name") or "", 0.0)

    return llm, tool


def _row_for_span(raw: dict) -> dict:
    kind = raw.get("openinference_kind")
    tool_name = raw.get("tool_name") if kind == "TOOL" else None
    classifier_tc = None
    if tool_name == "task_classifier":
        classifier_tc = _parse_classifier_output(raw.get("output_value"))
    llm_cost, tool_cost = _cost_for_span(raw)
    return {
        "span_id": raw["span_id"],
        "trace_id": raw["trace_id"],
        "parent_id": raw.get("parent_id"),
        "span_kind": kind,
        "name": raw.get("name"),
        "start_time": raw["start_time"],
        "end_time": raw.get("end_time"),
        "tool_name": tool_name,
        "classifier_task_class": classifier_tc,
        "cache_hit": 1 if raw.get("cache_hit") else 0,
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "reasoning_tokens": int(raw.get("reasoning_tokens") or 0),
        "model_name": _normalize_model(raw.get("model_name"))
            if kind == "LLM" else raw.get("model_name"),
        "llm_cost_usd": llm_cost,
        "tool_cost_usd": tool_cost,
    }


def _process_payload(payload_json: str) -> int:
    payload = json.loads(payload_json)
    spans = payload.get("spans", [])
    if not spans:
        return 0
    with connect() as c:
        c.execute("BEGIN")
        for raw in spans:
            upsert_span(_row_for_span(raw), conn=c)
        c.execute("COMMIT")
    return len(spans)


def _refresh_state() -> dict:
    """Recompute live_state from the spans table after a worker batch
    has landed. Writes to the same single live_state key the backfill
    uses, so the dashboard's UI elements all read from one source.

    Returns the detection state (with `anomalies`) so the caller can
    schedule Gemini reasoning on state changes.
    """
    state = run_detection()
    generate_templated_recommendations(state)

    # Preserve any in-progress backfill metadata so the banner
    # doesn't flicker if a backfill happens to be running alongside.
    existing_raw = get_meta("live_state")
    existing_ingest: dict = {"status": "idle"}
    if existing_raw:
        try:
            existing_ingest = json.loads(existing_raw).get("ingest", existing_ingest)
        except Exception:
            pass

    # Count spans directly (run_detection's trace list doesn't expose this).
    from governor.pipeline.db import connect as _connect, savings_summary
    with _connect() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM spans").fetchone()
        total_spans = int(row["n"] or 0)
    # Refactor #2: realized savings re-derived from Phoenix-sourced per-span
    # savings_usd (SUM over the cache, which mirrors Phoenix).
    saved = savings_summary()

    total_llm = sum(
        s["avg_llm_cost_usd"] * s["n"]
        for s in state["by_task_class"].values()
    )
    total_tool = sum(
        s["avg_tool_cost_usd"] * s["n"]
        for s in state["by_task_class"].values()
    )

    live = {
        "ingest": existing_ingest,
        "summary": {
            "total_traces": state["total_traces"],
            "total_spans": total_spans,
            "total_llm_cost_usd": round(total_llm, 6),
            "total_tool_cost_usd": round(total_tool, 6),
            "total_cost_usd": state["total_cost_usd"],
            "total_savings_usd": saved["total_savings_usd"],
            "spans_reconciled": saved["spans_reconciled"],
            "last_updated_at": state["now"],
        },
        "by_task_class": state["by_task_class"],
        "anomalies": state["anomalies"],
    }
    set_meta("live_state", json.dumps(live))
    return state


def reconcile_from_phoenix(max_spans: int = 4000) -> int:
    """Refactor #2: pull Phoenix's computed LLM cost + per-span savings and
    overwrite the cache's local-compute fallback, making Phoenix the source
    of truth. Cheap and bounded (newest-first sweep), idempotent, and safe
    to call repeatedly — Phoenix's cost-compute lag settles within a sweep
    or two. Returns the number of cache rows updated."""
    rows = phoenix_cost.fetch_span_costs(max_spans=max_spans)
    updated = update_phoenix_costs(rows)
    log.info("reconciled %s/%s spans from Phoenix", updated, len(rows))
    # Tag governed spans in Phoenix so the intervention is visible in the
    # trace view (Annotations tab). Once per span via the `annotated` flag.
    try:
        pending = unannotated_saving_spans()
        if pending:
            done = phoenix_cost.annotate_savings(pending)
            if done:
                mark_spans_annotated(done)
                log.info("annotated %s saving spans in Phoenix", len(done))
    except Exception:
        log.exception("Phoenix savings annotation failed")
    return updated


# How often the live loop reconciles cost against Phoenix (seconds).
RECONCILE_INTERVAL_S = 30.0


async def initial_refresh() -> None:
    """On startup, if the cache already has spans (an existing account,
    no backfill coming), recompute state + recommendations + reasoning
    once. Without this, a restart with a populated cache shows whatever
    recommendations were last written — stale if the analysis code
    changed and there's no new live traffic to trigger a refresh."""
    with connect() as c:
        n = int(c.execute("SELECT COUNT(*) AS n FROM spans").fetchone()["n"] or 0)
    if n == 0:
        return  # empty cache → onboarding backfill (re)generates everything
    log.info("initial refresh over %s existing spans", n)
    try:
        state = _refresh_state()
        reasoning.schedule_if_changed(state.get("issues", []))
    except Exception:
        log.exception("initial refresh failed")


async def run_forever(idle_sleep: float = 0.1, batch_size: int = 20) -> None:
    log.info("worker starting")
    last_reconcile = 0.0  # 0 ⇒ reconcile immediately on first iteration
    while True:
        # Refactor #2: periodically overwrite the local-compute cost fallback
        # with Phoenix's authoritative LLM cost + per-span savings. Runs off
        # the event loop so it never stalls the outbox drain, and fires even
        # when traffic is idle so Phoenix's cost-compute lag still settles.
        if time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_S:
            last_reconcile = time.monotonic()
            try:
                if await asyncio.to_thread(reconcile_from_phoenix):
                    await asyncio.to_thread(_refresh_state)
            except Exception:
                log.exception("Phoenix reconcile failed")

        try:
            batches = claim_pending_batches(limit=batch_size)
        except Exception:
            log.exception("claim_pending_batches failed")
            await asyncio.sleep(1.0)
            continue

        if not batches:
            await asyncio.sleep(idle_sleep)
            continue

        any_ok = False
        for b in batches:
            try:
                _process_payload(b["payload"])
                mark_batch_processed(b["id"])
                any_ok = True
            except Exception as e:
                log.exception("processing outbox row %s failed", b["id"])
                mark_batch_failed(b["id"], str(e))

        if any_ok:
            try:
                state = _refresh_state()
                # Tier 3: schedule Gemini reasoning if the issue picture
                # changed. Non-blocking — won't stall the loop.
                reasoning.schedule_if_changed(state.get("issues", []))
            except Exception:
                log.exception("state refresh failed")
