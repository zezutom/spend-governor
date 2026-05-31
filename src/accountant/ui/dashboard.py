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
    uv run streamlit run src/accountant/ui/dashboard.py
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

from accountant.pipeline.db import connect, get_meta


INGEST_HOST = "127.0.0.1"
INGEST_PORT = int(os.environ.get("ACCOUNTANT_INGEST_PORT", "8765"))
INGEST_URL = f"http://{INGEST_HOST}:{INGEST_PORT}"
LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "ingest_server.log"

# Cheapest task class — the cost-per-ticket baseline every other class is
# compared against.
BASELINE_CLASS = "password_reset"


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
            "accountant.pipeline.ingest_server:app",
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
            "FROM recommendations WHERE superseded = 0"
        ).fetchall()
    recs = [dict(r) for r in rows]

    # Always rank by impact (projected monthly savings), descending.
    # Applied/updated state must NOT change the order — the biggest
    # leak stays on top whether or not it's been actioned.
    def _impact(r: dict) -> float:
        try:
            return float(json.loads(r.get("data") or "{}").get("monthly_savings_usd") or 0)
        except Exception:
            return 0.0

    recs.sort(key=_impact, reverse=True)
    return recs


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


def _issue_of(rec: dict) -> dict:
    try:
        return json.loads(rec.get("data") or "{}")
    except Exception:
        return {}


def _policy_for_issue(issue: dict):
    """Map a detected issue to the wrapper policy that fixes it at
    runtime. Returns (signature, policy_type, params) or None."""
    kind = issue.get("kind")
    if kind == "tool_cache" and issue.get("primary_tool"):
        tool = issue["primary_tool"]
        unit = (issue.get("components") or {}).get("tool_unit_price_usd", 0.0)
        return (f"cache_tool:{tool}", "cache_tool",
                {"tool": tool, "cost_per_call_usd": unit})
    if kind == "model_routing":
        # Spread the per-ticket saving across ~4 LLM calls/ticket for a
        # per-call estimate the wrapper records on each downgrade.
        per_call = round((issue.get("savings_per_ticket_usd", 0) or 0) / 4.0, 6)
        return ("route_model:simple", "route_model",
                {"cheap_model": issue.get("cheap_model", "gemini-2.5-flash-lite"),
                 "est_savings_per_call_usd": per_call})
    return None


def _render_realized_savings() -> None:
    # Both the dollar figure AND the intervention count come from
    # Phoenix-reconciled per-span savings (refactor #2), so they describe
    # the SAME population — a customer can verify both from their own
    # traces. (The wrapper's interventions log is no longer the source:
    # it counts every governed call incl. traffic not in this dataset.)
    from accountant.pipeline.db import savings_summary
    saved = savings_summary()
    realized = saved["total_savings_usd"]
    n = saved["spans_with_savings"]
    if realized <= 0 and n == 0:
        st.caption(
            "No realized savings yet — activate a policy below, then run "
            "the observed agent to see the wrapper intervene live."
        )
        return
    c1, c2 = st.columns(2)
    c1.metric("Realized savings (from Phoenix)", f"${realized:,.4f}")
    c2.metric("Saving interventions", f"{n:,}")
    parts = []
    if saved["cache_hits"]:
        parts.append(f"{saved['cache_hits']} cache hits")
    if saved["model_swaps"]:
        parts.append(f"{saved['model_swaps']} model downgrades")
    if parts:
        st.caption("In analyzed traces: " + "  ·  ".join(parts))
    st.caption(
        "This is Σ of per-span savings (Phoenix-sourced). The **proof it's "
        "real** is the per-policy before/after below — cost-per-ticket "
        "dropping across your actual traffic, measured from Phoenix."
    )


def _affected_classes(issue: dict) -> list[str]:
    if issue.get("kind") == "model_routing":
        return list(issue.get("classes") or [])
    tc = issue.get("task_class")
    return [tc] if tc else []


def _render_verification(issue: dict, policy_sig: str) -> None:
    from accountant.analytics.verification import measured_before_after
    from accountant.wrapper import store as gov_store

    classes = _affected_classes(issue)
    since = gov_store.policy_activated_at(policy_sig)
    m = measured_before_after(classes, since)

    if not m["has_after_data"]:
        st.info(
            "▶ Policy is live. Send traffic through the agent and the "
            "**measured** before/after cost will appear here — proven from "
            "your own traces, not estimated."
        )
        return

    pct = int(round(m["pct_reduction"] * 100))
    st.success(
        f"**Verified from your traces:** {m['before_n']:,} tickets before vs "
        f"{m['after_n']:,} since activation — cost-per-ticket "
        f"**\\${m['before_avg_usd']:.4f} → \\${m['after_avg_usd']:.4f}** "
        f"(**−{pct}%**), **\\${m['measured_savings_usd']:,.4f}** saved so far."
    )


_PROJECT_GID: str | None = None


def _project_gid() -> str | None:
    """Phoenix project node id for span deeplinks. Module-cached so a
    transient None is retried next render — st.cache_data would pin the
    None forever and silently kill the verify links."""
    global _PROJECT_GID
    if not _PROJECT_GID:
        from accountant.pipeline.phoenix_cost import project_gid
        _PROJECT_GID = project_gid()
    return _PROJECT_GID


def _story_cause(issue: dict) -> str:
    """Plain-English cause + fix — the line that triggers the AHA."""
    comp = issue.get("components") or {}
    if issue.get("kind") == "model_routing":
        cheap = issue.get("cheap_model", "a cheaper model")
        return (f"Simple tickets (password resets, FAQs) ran on the standard model. "
                f"Routed to **{cheap}** at the gateway — same answer, far cheaper input.")
    tool = issue.get("primary_tool") or "the tool"
    removed = round(comp.get("avg_tool_calls_removed", 0) or 0)
    return (f"Tickets re-ran the **same `{tool}`** ~{removed}× — pure waste. "
            f"Now served from a semantic cache; the paid call never fires.")


def _render_policy_proof(kind: str, sig: str, saved: dict, gid: str | None) -> None:
    """The cumulative proof for ONE policy: an aggregated savings timeline
    (the curve) + a paginated, Phoenix-linked list of the individual spans
    that saved. Aggregate lives here (our dashboard); Phoenix proves each
    part (per-span deeplinks)."""
    import math
    import pandas as pd
    from accountant.pipeline.db import policy_savings_series, policy_saving_spans
    from accountant.pipeline.phoenix_cost import span_deeplink

    ch = (kind == "tool_cache")
    total = (saved.get("cache_hits") if ch else saved.get("model_swaps")) or 0
    if total == 0:
        return

    # Aggregated view — cumulative savings over time.
    series = policy_savings_series(ch)
    if series:
        df = pd.DataFrame(series)
        df["t"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True)
        df = df.dropna(subset=["t"]).sort_values("t")
        if not df.empty:
            df["Cumulative saved (USD)"] = df["savings_usd"].cumsum()
            st.caption("Cumulative savings over time")
            st.area_chart(df.set_index("t")[["Cumulative saved (USD)"]], height=160)

    # Drill-down — paginated list, each row opens that span in Phoenix.
    with st.expander(f"Verify in Phoenix — the {total} spans that saved (paginated)"):
        PAGE = 25
        npages = max(1, math.ceil(total / PAGE))
        page = int(st.number_input("Page", 1, npages, 1, key=f"pg_{sig}"))
        rows = policy_saving_spans(ch, limit=PAGE, offset=(page - 1) * PAGE)
        table = pd.DataFrame([{
            "when (UTC)": str(r.get("start_time", ""))[:19],
            "what": r.get("tool_name") or r.get("model_name") or "",
            "saved (USD)": round(r.get("savings_usd", 0) or 0, 6),
            "verify": (span_deeplink(gid, r.get("trace_id"), r.get("phoenix_node_id"))
                       if gid else None),
        } for r in rows])
        st.dataframe(
            table, hide_index=True, use_container_width=True,
            column_config={
                "saved (USD)": st.column_config.NumberColumn(format="$%.6f"),
                "verify": st.column_config.LinkColumn("verify", display_text="open span ↗"),
            },
        )
        st.caption(f"Page {page} of {npages} · each row opens that span in Phoenix "
                   "(its `accountant.savings` annotation is under the Annotations tab).")


def _render_tool_pricing() -> None:
    """Read-only view of the operator-configured per-call tool rates. Phoenix
    prices LLM tokens but NOT tools, so tool dollars = Phoenix-measured call
    count × these configured rates. Surfacing them keeps the dollar figures
    honest (the rates are an input, not a Phoenix measurement)."""
    import pandas as pd
    from accountant.pricing.tools import TOOL_PRICES
    df = pd.DataFrame(
        [{"tool": k, "$ / call": v} for k, v in
         sorted(TOOL_PRICES.items(), key=lambda kv: -kv[1])]
    )
    st.dataframe(df, hide_index=True, use_container_width=True, column_config={
        "$ / call": st.column_config.NumberColumn(format="$%.4f"),
    })
    st.caption(
        "Operator-configured tool rates (read-only for now — edit "
        "`src/accountant/pricing/tools.py`). Phoenix prices LLM tokens, not "
        "tools, so **tool dollars = call count (measured in Phoenix) × these "
        "rates**. The rates are an input you set, not a Phoenix measurement."
    )


def _render_waste_breakdown(recs: list[dict], gid: str | None) -> None:
    """Full traceability for the headline 'Avoidable AI waste': a per-pattern
    table, the exact projection formula (so 'assumed based on what' is answered
    in the UI), and a paginated, Phoenix-linked list of the actual class traces
    behind each per-ticket figure (which is an average, not a fixed price)."""
    import math
    import pandas as pd
    from accountant.pipeline.db import class_cost_stats, class_trace_costs
    from accountant.pipeline.phoenix_cost import span_deeplink

    items = [(r, _issue_of(r)) for r in recs]
    items = [(r, i) for r, i in items if (i.get("savings_per_ticket_usd", 0) or 0) > 0]
    if not items:
        return
    total_mo = sum((i.get("monthly_savings_usd", 0) or 0) for _, i in items)
    window = (items[0][1].get("components") or {}).get("window_days", "?")

    st.markdown("#### Avoidable AI waste — how it's calculated")
    table = pd.DataFrame([{
        "pattern": r["title"][:38],
        "tickets seen": i.get("n_traces", 0),
        "avg now/ticket": round(i.get("current_avg_usd", 0) or 0, 5),
        "avg after/ticket": round(i.get("projected_avg_usd", 0) or 0, 5),
        "saving/ticket (measured)": round(i.get("savings_per_ticket_usd", 0) or 0, 5),
        "tickets/mo (assumed)": i.get("monthly_volume", 0),
        "waste/mo (projected)": round(i.get("monthly_savings_usd", 0) or 0, 2),
    } for r, i in items])
    st.dataframe(table, hide_index=True, use_container_width=True, column_config={
        "avg now/ticket": st.column_config.NumberColumn(format="$%.5f"),
        "avg after/ticket": st.column_config.NumberColumn(format="$%.5f"),
        "saving/ticket (measured)": st.column_config.NumberColumn(format="$%.5f"),
        "waste/mo (projected)": st.column_config.NumberColumn(format="$%.2f"),
    })
    st.markdown(
        f"**Total projected: \\${total_mo:,.2f}/mo.** Each `waste/mo` = "
        f"`saving/ticket × tickets/mo`. The first three columns are **measured** "
        f"from your traces; **`tickets/mo` is assumed** = "
        f"`tickets seen ÷ window_days × 30`, where "
        f"`window_days = max(observed trace span, 0.5)`."
    )
    st.warning(
        f"⚠️ Your traces span far less than a day, so `window_days` hit its "
        f"**0.5-day floor** (currently {window}). So `/mo` assumes this sample ≈ "
        f"half a day of production traffic — a **hypothesis at an assumed volume, "
        f"not a measured rate**. (Patterns can also overlap on shared ticket "
        f"types, so the total is an upper bound.)"
    )

    with st.expander("Tool pricing (configured) — what the tool dollars are based on"):
        _render_tool_pricing()

    for rec, issue in items:
        classes = _affected_classes(issue)
        if not classes:
            continue
        spt = issue.get("savings_per_ticket_usd", 0) or 0
        stats = class_cost_stats(classes)
        label = ", ".join(classes)
        with st.expander(f"Prove the \\${spt:.5f}/ticket — the {stats['n']} “{label}” tickets behind it"):
            st.caption(
                f"It's the **average**, not a fixed price: these tickets cost "
                f"**\\${stats['min']:.5f}–\\${stats['max']:.5f}** each "
                f"(avg \\${stats['avg']:.5f}). Open any in Phoenix to see its real "
                f"cost and the redundant calls."
            )
            PAGE = 20
            npages = max(1, math.ceil(stats["n"] / PAGE))
            page = int(st.number_input("Page", 1, npages, 1, key=f"wpg_{rec['signature']}"))
            traces = class_trace_costs(classes, PAGE, (page - 1) * PAGE)
            df = pd.DataFrame([{
                "trace": (t.get("trace_id") or "")[:12] + "…",
                "LLM $ (Phoenix)": round(t.get("llm_cost", 0) or 0, 6),
                "web_search calls": t.get("n_ws", 0),
                "tool $ (calls × rate)": round(t.get("tool_cost", 0) or 0, 6),
                "total $": round((t.get("llm_cost", 0) or 0) + (t.get("tool_cost", 0) or 0), 6),
                "open in Phoenix": (span_deeplink(gid, t.get("trace_id"), None)
                                    if gid else None),
            } for t in traces])
            st.dataframe(df, hide_index=True, use_container_width=True, column_config={
                "LLM $ (Phoenix)": st.column_config.NumberColumn(format="$%.6f"),
                "tool $ (calls × rate)": st.column_config.NumberColumn(format="$%.6f"),
                "total $": st.column_config.NumberColumn(format="$%.6f"),
                "open in Phoenix": st.column_config.LinkColumn(
                    "open in Phoenix", display_text="open trace ↗"),
            })
            st.caption(
                f"**LLM $** = the trace's cost **measured by Phoenix** (its header). "
                f"**tool $** = `web_search calls` (counted in Phoenix) × your configured "
                f"rate (Tool pricing above) — Phoenix doesn't price tools. So Phoenix's "
                f"header shows the LLM column; total = LLM + tool. Open a trace to confirm "
                f"the LLM cost and count the web_search calls. · Page {page}/{npages}."
            )


def _default_ws_rate() -> float:
    from accountant.pricing.tools import TOOL_PRICES
    return TOOL_PRICES.get("web_search", 0.005)


def _observed_hours(recs: list[dict]) -> float:
    for r in recs:
        wd = (_issue_of(r).get("components") or {}).get("window_days")
        if wd:
            return wd * 24
    return 12.0


def _default_monthly_tickets(live: dict, recs: list[dict]) -> int:
    by = live.get("by_task_class") or {}
    total_n = int((live.get("summary") or {}).get("total_traces") or 0) or sum(s["n"] for s in by.values())
    wd = 0.5
    for r in recs:
        wd = (_issue_of(r).get("components") or {}).get("window_days") or wd
        break
    return int(round(total_n / max(wd, 1e-9) * 30)) if total_n else 0


def _issue_rows(live: dict, recs: list[dict], ws_rate: float):
    """Per-class derived view + page totals, recomputed from the live
    web_search rate. Reuses stored measured values; only the web_search-priced
    parts move with the rate. Returns (rows, totals)."""
    by = live.get("by_task_class") or {}
    total_n = int((live.get("summary") or {}).get("total_traces") or 0) or sum(s["n"] for s in by.values())
    cache_rec, routing = {}, None
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            cache_rec[i["task_class"]] = i
        elif i.get("kind") == "model_routing":
            routing = i
    base_cost = (by.get(BASELINE_CLASS) or {}).get("avg_cost_usd") or 1e-9
    d_ws = ws_rate - _default_ws_rate()
    rows, rec_tot, cost_tot = [], 0.0, 0.0
    for tc, s in by.items():
        n = s["n"]; avg_ws = s.get("avg_web_search", 0) or 0
        llm = s["avg_llm_cost_usd"]; tool = s["avg_tool_cost_usd"] + avg_ws * d_ws
        cost = llm + tool
        saving = 0.0
        ci = cache_rec.get(tc)
        if ci:
            comp = ci.get("components") or {}
            if ci.get("primary_tool") == "web_search":
                ts = (comp.get("avg_tool_calls_removed", 0) or 0) * ws_rate
            else:
                ts = comp.get("tool_savings_per_ticket_usd", 0) or 0
            ls = (ci.get("savings_per_ticket_usd", 0) or 0) - (comp.get("tool_savings_per_ticket_usd", 0) or 0)
            saving += ts + ls
        if routing and tc in (routing.get("classes") or []):
            saving += routing.get("savings_per_ticket_usd", 0) or 0
        rows.append({"tc": tc, "n": n, "cost": cost, "llm": llm, "tool": tool,
                     "saving": saving, "mult": cost / base_cost,
                     "is_base": tc == BASELINE_CLASS})
        rec_tot += saving * n; cost_tot += cost * n
    for r in rows:
        r["share"] = (r["cost"] * r["n"]) / (cost_tot or 1e-9)
    rows.sort(key=lambda r: r["cost"] * r["n"], reverse=True)
    cpt = cost_tot / total_n if total_n else 0
    rpt = rec_tot / total_n if total_n else 0
    return rows, {"total_n": total_n, "cost_per_ticket": cpt,
                  "recoverable_per_ticket": rpt,
                  "pct_avoidable": (rpt / cpt) if cpt else 0}


def _badge(text: str, danger: bool) -> str:
    bg, fg = ("#fde7e6", "#b3261e") if danger else ("#e3f4e8", "#1e7a3c")
    return (f"<span style='background:{bg};color:{fg};padding:1px 8px;border-radius:10px;"
            f"font-size:0.78rem;font-weight:700;white-space:nowrap'>{text}</span>")


def _bar(llm: float, tool: float, maxv: float) -> str:
    lw = (llm / maxv * 100) if maxv else 0
    tw = (tool / maxv * 100) if maxv else 0
    hatch = ("repeating-linear-gradient(45deg,#e08a3c,#e08a3c 4px,#f3c89a 4px,#f3c89a 8px)")
    return (f"<div style='display:flex;height:18px;border-radius:4px;overflow:hidden;"
            f"background:#eee;margin:2px 0 10px'>"
            f"<div title='tokens (Phoenix)' style='width:{lw:.2f}%;background:#3b6fd4'></div>"
            f"<div title='tools (count × rate)' style='width:{tw:.2f}%;background:{hatch}'></div>"
            f"</div>")


def _class_reasons(recs: list[dict]) -> dict:
    out: dict = {}
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            comp = i.get("components") or {}
            n = round(comp.get("avg_tool_calls_removed", 0) or 0)
            out.setdefault(i["task_class"], []).append(
                f"re-runs the same `{i.get('primary_tool', 'tool')}` ~{n}×")
        if i.get("kind") == "model_routing":
            for c in (i.get("classes") or []):
                out.setdefault(c, []).append("runs on the full-price model")
    return {tc: " and ".join(parts) + "." for tc, parts in out.items()}


def _policy_mo(issue: dict, ws_rate: float, mt: int, total_n: int) -> float:
    n = issue.get("n_traces", 0) or 0
    if issue.get("kind") == "tool_cache" and issue.get("primary_tool") == "web_search":
        comp = issue.get("components") or {}
        spt = ((comp.get("avg_tool_calls_removed", 0) or 0) * ws_rate
               + ((issue.get("savings_per_ticket_usd", 0) or 0)
                  - (comp.get("tool_savings_per_ticket_usd", 0) or 0)))
    else:
        spt = issue.get("savings_per_ticket_usd", 0) or 0
    return spt * mt * (n / total_n if total_n else 0)


def _fix_text(issue: dict):
    if issue.get("kind") == "model_routing":
        pct = int(round((issue.get("pct_reduction", 0) or 0) * 100))
        return ("Route simple tickets to a cheaper model",
                f"Resets and FAQs go to `{issue.get('cheap_model', 'a cheaper model')}` "
                f"— same answer, {pct}% less.")
    tool = issue.get("primary_tool", "the tool")
    cls = (issue.get("task_class", "") or "").replace("_", " ")
    return (f"Cache the repeated {cls} lookups",
            f"Serve the duplicate `{tool}` from cache — the paid call never fires.")


def _render_bill(recs, rows, totals, mt, n_active, realized, default_mt) -> None:
    pct = int(round(totals["pct_avoidable"] * 100))
    cpt = totals["cost_per_ticket"]
    rec_mo = totals["recoverable_per_ticket"] * mt
    st.markdown("<div style='color:#777;font-size:0.95rem'>This agent's support "
                "ticket spend</div>", unsafe_allow_html=True)
    head, ctrl = st.columns([3, 2])
    with head:
        if n_active == 0:
            st.markdown(
                f"<div style='font-size:3rem;font-weight:800;color:#c0392b;line-height:1.05'>"
                f"−{pct}%</div><div style='color:#333'>of every dollar per ticket is "
                f"avoidable</div>", unsafe_allow_html=True)
            st.markdown(
                f"**\\${cpt:.4f}** per ticket now · **\\${rec_mo:,.0f}/mo** recoverable "
                f"<span style='color:#999;font-size:0.85rem'>(upper bound — patterns "
                f"overlap)</span>", unsafe_allow_html=True)
        else:
            st.markdown(
                f"<div style='font-size:2.1rem;font-weight:800;color:#1e7a3c;line-height:1.1'>"
                f"\\${realized:,.4f} saved so far</div>"
                f"<div style='color:#333'>~\\${rec_mo:,.0f}/mo projected once fully governed "
                f"· {pct}% of per-ticket spend still avoidable</div>", unsafe_allow_html=True)
    with ctrl:
        st.slider("Your monthly tickets", min_value=1000,
                  max_value=max(100000, int(default_mt * 3)), step=1000, key="monthly_tickets")
    st.caption(
        f"Measured over {totals['total_n']:,} real tickets (~{_observed_hours(recs):.0f}h of "
        f"traffic). Everything above is a measured per-ticket ratio × the volume you set — "
        f"not an assumed rate.")


def _render_where(rows, recs) -> None:
    st.subheader("Where it goes — and why")
    reasons = _class_reasons(recs)
    for r in rows:
        if r["tc"] == "unknown":  # unclassified partial traces — not a task type
            continue
        danger = (not r["is_base"]) and r["mult"] >= 2.0
        badge = _badge(f"{r['share'] * 100:.0f}% · {r['mult']:.1f}×", danger)
        if r["is_base"]:
            why = "already at baseline — nothing to fix here."
        else:
            why = reasons.get(r["tc"]) or "near baseline — nothing worth fixing."
        st.markdown(
            f"{badge}&nbsp;&nbsp;**{r['tc'].replace('_', ' ').title()}** — {why}",
            unsafe_allow_html=True)


def _render_decomposition(rows) -> None:
    st.subheader("What one ticket actually costs")
    hatch = "repeating-linear-gradient(45deg,#e08a3c,#e08a3c 3px,#f3c89a 3px,#f3c89a 6px)"
    st.markdown(
        "<span style='font-size:0.85rem;color:#555'>Two parts, different certainty: "
        "<span style='background:#3b6fd4;color:#fff;padding:0 6px;border-radius:3px'>tokens</span> "
        "metered by Phoenix · <span style='background:" + hatch +
        ";padding:0 6px;border-radius:3px'>tools</span> = call count (Phoenix) × your "
        "configured rate (below).</span>", unsafe_allow_html=True)
    maxv = max((r["cost"] for r in rows), default=1e-9)
    for r in rows:
        st.markdown(
            f"**{r['tc'].replace('_', ' ').title()}** "
            f"<span style='color:#888'>\\${r['cost']:.4f}</span>", unsafe_allow_html=True)
        st.markdown(_bar(r["llm"], r["tool"], maxv), unsafe_allow_html=True)
    c = st.columns([2, 3])
    with c[0]:
        st.number_input("✎ web_search rate $/call", min_value=0.0, step=0.001,
                        format="%.4f", key="ws_rate")
    with c[1]:
        st.caption("This drives most of the refund-handling cost. If it's wrong, the "
                   "headline is wrong. It's operator-set, not a Phoenix measurement.")


def _render_fixes(recs, mt, ws, total_n, gov_store) -> None:
    st.subheader("The fixes")
    items = []
    for r in recs:
        i = _issue_of(r)
        p = _policy_for_issue(i)
        if p and (i.get("savings_per_ticket_usd", 0) or 0) > 0:
            items.append((p, i, r))
    if not items:
        st.success("No cost issues detected — the agent is running clean.")
        return
    total_proj = 0.0
    for policy, issue, rec in items:
        sig = policy[0]
        active = gov_store.is_active(sig)
        mo = _policy_mo(issue, ws, mt, total_n)
        total_proj += mo
        title, reason = _fix_text(issue)
        with st.container(border=True):
            c = st.columns([5, 2])
            with c[0]:
                st.markdown(f"### {title}")
                st.markdown(reason)
                tag = "✅ governing live · " if active else ""
                st.markdown(
                    f"<span style='color:#1e7a3c;font-weight:700'>saves \\${mo:,.0f}/mo</span> "
                    f"<span style='color:#888'>· {tag}runtime only, reversible</span>",
                    unsafe_allow_html=True)
            with c[1]:
                if active:
                    if st.button("Turn off", key=f"deact_{sig}", use_container_width=True):
                        gov_store.deactivate_policy(sig)
                        st.rerun()
                else:
                    if st.button("Activate ↗", key=f"act_{sig}", type="primary",
                                 use_container_width=True):
                        gov_store.activate_policy(sig, policy[1], policy[2])
                        st.rerun()
    st.markdown(
        f"<div style='display:flex;justify-content:space-between;align-items:center;"
        f"background:#f3f5ee;padding:10px 16px;border-radius:8px;margin-top:4px'>"
        f"<span style='font-weight:600'>Activate all three</span>"
        f"<span style='color:#1e7a3c;font-weight:800;font-size:1.3rem'>\\${total_proj:,.0f}/mo</span>"
        f"</div>", unsafe_allow_html=True)


def _render_math_drawer(recs, gid) -> None:
    from accountant.pipeline.db import savings_summary
    from accountant.wrapper import store as gov_store
    saved = savings_summary()
    with st.expander("Show the math — Phoenix traces, per-ticket proof, tool rates"):
        _render_realized_savings()
        st.divider()
        _render_waste_breakdown(recs, gid)
        st.divider()
        st.markdown("##### Per-policy savings detail")
        for rec in recs:
            issue = _issue_of(rec)
            policy = _policy_for_issue(issue)
            if not policy:
                continue
            sig = policy[0]
            st.markdown(f"**{rec['title']}**")
            _render_policy_proof(issue.get("kind"), sig, saved, gid)
            if gov_store.is_active(sig):
                _render_verification(issue, sig)


@st.fragment(run_every="2s")
def render_dashboard() -> None:
    # ONE fragment so a slider/rate change reruns the whole report and every
    # section reflects it. The 2s heartbeat keeps realized numbers live during
    # governed traffic without fighting slider drags or racing button clicks.
    _ensure_ingest_server()
    live = _load_live_state()
    status = (live.get("ingest") or {}).get("status")
    span_count = _cache_span_count()
    if span_count == 0 and status not in ("in_progress", "complete"):
        _post_backfill_start()
        time.sleep(0.3)
        live = _load_live_state()
        status = (live.get("ingest") or {}).get("status")
    if (span_count == 0 or status in ("in_progress", "starting", None)) and status != "complete":
        _render_onboarding(live)
        return

    from accountant.wrapper import store as gov_store
    from accountant.wrapper.store import active_policies
    from accountant.pipeline.db import savings_summary

    recs = _load_recommendations()
    default_mt = _default_monthly_tickets(live, recs)
    st.session_state.setdefault("monthly_tickets", default_mt)
    st.session_state.setdefault("ws_rate", _default_ws_rate())
    mt = st.session_state["monthly_tickets"]
    ws = st.session_state["ws_rate"]

    rows, totals = _issue_rows(live, recs, ws)
    n_active = len(active_policies())
    realized = savings_summary().get("total_savings_usd", 0) or 0
    gid = _project_gid()

    _render_bill(recs, rows, totals, mt, n_active, realized, default_mt)
    st.divider()
    _render_where(rows, recs)
    st.divider()
    _render_decomposition(rows)
    st.divider()
    _render_fixes(recs, mt, ws, totals["total_n"], gov_store)
    st.divider()
    _render_math_drawer(recs, gid)


def main() -> None:
    st.title("Agent Accountant")
    st.caption("Live unit economics for the observed agent — measured from Phoenix.")
    render_dashboard()


main()
