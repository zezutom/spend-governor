"""Streamlit dashboard for the Accountant.

Single entry point — running this file spins up the entire stack:

1. Starts the ingest server (FastAPI on :8765) as a background subprocess
   if it's not already running.
2. Reads the SQLite cache. If empty (new-account onboarding), triggers a
   Phoenix backfill via POST /backfill/start. Watches the backfill
   progress live.
3. Once data is present, renders by-class cost aggregates and the
   active recommendation cards. Auto-refreshes every 2 seconds.

Run:
    uv run streamlit run src/accountant/dashboard.py
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from accountant.db import connect, get_meta


INGEST_HOST = "127.0.0.1"
INGEST_PORT = int(os.environ.get("ACCOUNTANT_INGEST_PORT", "8765"))
INGEST_URL = f"http://{INGEST_HOST}:{INGEST_PORT}"
LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "ingest_server.log"


st.set_page_config(
    page_title="Agent Accountant",
    layout="wide",
)


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _ensure_ingest_server() -> None:
    """Spawn the FastAPI ingest server as a subprocess if nothing is on
    its port. Idempotent per Streamlit session — guards via a flag in
    st.session_state so script reruns don't keep checking.
    """
    if st.session_state.get("ingest_server_checked"):
        return
    if _port_open(INGEST_HOST, INGEST_PORT):
        st.session_state["ingest_server_checked"] = True
        return

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_PATH, "a")
    subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "accountant.ingest_server:app",
            "--host", INGEST_HOST,
            "--port", str(INGEST_PORT),
            "--log-level", "info",
        ],
        stdout=log_f,
        stderr=log_f,
        start_new_session=True,
    )

    for _ in range(40):
        if _port_open(INGEST_HOST, INGEST_PORT):
            break
        time.sleep(0.25)

    st.session_state["ingest_server_checked"] = True


def _post_backfill_start() -> None:
    """POST /backfill/start. Server is idempotent — safe to call on
    every fragment refresh."""
    try:
        httpx.post(f"{INGEST_URL}/backfill/start", timeout=3.0)
    except Exception:
        pass


def _cache_span_count() -> int:
    try:
        with connect() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM spans").fetchone()
            return int(row["n"] or 0)
    except Exception:
        return 0


def _load_live_state() -> dict:
    """Single source of truth — read one blob, render every UI element
    from it. No divergent cadences between counters and aggregates."""
    raw = get_meta("live_state")
    if not raw:
        return {
            "ingest": {"status": "idle"},
            "summary": {"total_traces": 0, "total_spans": 0, "total_cost_usd": 0.0},
            "by_task_class": {},
            "anomalies": [],
        }
    try:
        return json.loads(raw)
    except Exception:
        return {
            "ingest": {"status": "idle"},
            "summary": {"total_traces": 0, "total_spans": 0, "total_cost_usd": 0.0},
            "by_task_class": {},
            "anomalies": [],
        }


def _load_recommendations() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT signature, source, task_class, anomaly_type, title, "
            "description, data, updated_at "
            "FROM recommendations WHERE superseded = 0 "
            "ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _render_onboarding(live: dict) -> None:
    """The 'new account' experience: prominent banner, live progress.
    Reads only from `live` (the single live_state blob) so every
    counter is consistent with every other counter."""
    ingest = live.get("ingest") or {}
    summary = live.get("summary") or {}

    traces = int(summary.get("total_traces") or 0)
    spans = int(summary.get("total_spans") or 0)
    total_cost = float(summary.get("total_cost_usd") or 0.0)
    message = ingest.get("message") or "Connecting to Phoenix…"
    lookback = ingest.get("lookback_human") or ""
    estimated_total = ingest.get("estimated_total_traces")

    with st.container(border=True):
        st.markdown("### 👋 New account detected")
        st.markdown(
            "Importing your trace history from Phoenix. Counters update "
            "live as data arrives."
        )

        # Three headline numbers — the only metrics that matter to the
        # user: how many traces, how much money, how many individual
        # spans. Chunk numbers and UTC timestamps are deliberately
        # absent.
        traces_label = (
            f"{traces:,} / ~{estimated_total:,}"
            if estimated_total
            else f"{traces:,}"
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Traces imported", traces_label)
        c2.metric("Total cost (USD)", f"${total_cost:,.4f}")
        c3.metric("Spans imported", f"{spans:,}")

        if estimated_total and estimated_total > 0:
            est_progress = min(traces / estimated_total, 1.0)
            st.progress(est_progress, text=message)
        elif ingest.get("mode") == "exhaustive":
            chunks = int(ingest.get("processed_chunks") or 0)
            fake = min(0.05 + (chunks / 200.0), 0.95)
            st.progress(fake, text=message)
        else:
            progress = float(ingest.get("progress") or 0.0)
            st.progress(min(max(progress, 0.0), 1.0), text=message)

        if lookback:
            st.caption(f"Currently reviewing activity from {lookback}.")


def _render_header(live: dict) -> None:
    summary = live.get("summary") or {}
    total_traces = int(summary.get("total_traces") or 0)
    total_cost = float(summary.get("total_cost_usd") or 0.0)
    last_updated = summary.get("last_updated_at") or "—"

    c1, c2, c3 = st.columns(3)
    c1.metric("Traces ingested", f"{total_traces:,}")
    c2.metric("Total cost (USD)", f"${total_cost:.4f}")
    c3.metric(
        "Last update (UTC)",
        last_updated.split("T")[1][:8] if "T" in last_updated else last_updated,
    )


def _render_by_class(live: dict) -> None:
    by_class = live.get("by_task_class") or {}
    if not by_class:
        st.info("Waiting for data…")
        return

    rows = []
    for tc, s in by_class.items():
        rows.append({
            "Task class": tc,
            "Traces": s["n"],
            "Avg cost (USD)": s["avg_cost_usd"],
            "Avg LLM cost": s["avg_llm_cost_usd"],
            "Avg tool cost": s["avg_tool_cost_usd"],
            "Avg tools/trace": s["avg_tools"],
            "Avg web_search/trace": s["avg_web_search"],
            "Traces ≥3 web_search": s["traces_with_3plus_web_search"],
        })
    df = pd.DataFrame(rows).sort_values("Avg cost (USD)", ascending=False)
    st.dataframe(df, hide_index=True, use_container_width=True)


def _render_recommendations(recs: list[dict]) -> None:
    if not recs:
        st.success("No anomalies detected.")
        return

    st.caption(f"{len(recs)} active recommendation(s).")
    for rec in recs:
        with st.container(border=True):
            top = st.columns([6, 1])
            with top[0]:
                badge = "🤖 reasoned" if rec["source"] == "gemini" else "📋 pattern"
                st.markdown(f"**{rec['title']}**  ·  *{badge}*")
                st.write(rec["description"])
                if rec.get("data"):
                    try:
                        with st.expander("Supporting data"):
                            st.json(json.loads(rec["data"]))
                    except Exception:
                        pass
            with top[1]:
                st.caption(f"updated\n{rec['updated_at']}")


@st.fragment(run_every="500ms")
def render_live() -> None:
    # Ensure the ingest server is up. Cheap port probe + subprocess
    # spawn on the first fragment refresh only.
    _ensure_ingest_server()

    # ONE read. ONE source. Every UI element below derives from this
    # single blob.
    live = _load_live_state()
    ingest = live.get("ingest") or {}
    backfill_status = ingest.get("status")

    span_count = _cache_span_count()

    # New account: empty cache. Trigger backfill if it hasn't started.
    if span_count == 0 and backfill_status not in ("in_progress", "complete"):
        _post_backfill_start()
        time.sleep(0.3)
        live = _load_live_state()
        ingest = live.get("ingest") or {}
        backfill_status = ingest.get("status")

    recs = _load_recommendations()

    # Banner policy: while a backfill is running OR the cache is empty,
    # show the big onboarding banner ONLY — no header, no by-class
    # table, no recs. After backfill completes, the banner disappears
    # and the regular dashboard takes over. The same `live` blob feeds
    # both modes, so no cadence divergence is possible.
    show_onboarding = (
        span_count == 0
        or backfill_status in ("in_progress", "starting", None)
    ) and backfill_status != "complete"

    if show_onboarding:
        _render_onboarding(live)
        return

    _render_header(live)
    st.divider()

    st.subheader("Cost by task class")
    _render_by_class(live)
    st.divider()

    st.subheader("Active recommendations")
    _render_recommendations(recs)


def main() -> None:
    st.title("Agent Accountant")
    st.caption(
        "Live unit economics for the observed agent. "
        "Updates every 2 seconds as new spans arrive."
    )
    render_live()


main()
