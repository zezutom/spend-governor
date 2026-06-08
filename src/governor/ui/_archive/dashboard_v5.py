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
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

from governor.pipeline.db import connect, get_meta


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
            "governor.pipeline.ingest_server:app",
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
    from governor.pipeline.db import savings_summary
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
    from governor.analytics.verification import measured_before_after
    from governor.wrapper import store as gov_store

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
        from governor.pipeline.phoenix_cost import project_gid
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
    from governor.pipeline.db import policy_savings_series, policy_saving_spans
    from governor.pipeline.phoenix_cost import span_deeplink

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


def _render_tool_pricing(rates: dict) -> None:
    """Editable per-call tool rates (refinement #3). Phoenix prices LLM tokens
    but NOT tools, so tool dollars = Phoenix-measured call count × these
    operator-set rates. Editing any rate mutates the shared `rates` dict in
    session state; the next fragment rerun recomputes every bar and headline."""
    st.markdown("**Tool rates** — $/call. Phoenix counts the calls; you set the price.")
    cols = st.columns(3)
    for i, tool in enumerate(sorted(rates, key=lambda k: -rates[k])):
        with cols[i % 3]:
            rates[tool] = st.number_input(
                tool, min_value=0.0, step=0.0005, format="%.4f", value=float(rates[tool]))
    st.caption(
        "These are an **input you set**, not a Phoenix measurement. **Tool dollars = "
        "call count (measured in Phoenix) × these rates.** Changing one recomputes the "
        "bars and the headline above."
    )


def _render_waste_breakdown(recs: list[dict], gid: str | None) -> None:
    """Full traceability for the headline 'Avoidable AI waste': a per-pattern
    table, the exact projection formula (so 'assumed based on what' is answered
    in the UI), and a paginated, Phoenix-linked list of the actual class traces
    behind each per-ticket figure (which is an average, not a fixed price)."""
    import math
    import pandas as pd
    from governor.pipeline.db import class_cost_stats, class_trace_costs
    from governor.pipeline.phoenix_cost import span_deeplink

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


# --- Color discipline (refinement #5) -------------------------------------
# success green = money/savings · blue = measured tokens · amber = estimated
# tools + cost-vs-baseline badges. No large red surfaces anywhere.
_GREEN = "#15803d"
_BLUE = "#2563eb"
_AMBER = "#b45309"
_HATCH = "repeating-linear-gradient(45deg,#e0a44a,#e0a44a 4px,#f5d9a8 4px,#f5d9a8 8px)"


def _default_tool_rates() -> dict:
    """Operator-set per-call rates, editable in the math drawer. Drops the
    structural zero-cost classifier — it's not a billable tool."""
    from governor.pricing.tools import TOOL_PRICES
    return {k: float(v) for k, v in TOOL_PRICES.items() if k != "task_classifier"}


def _tool_cost(counts: dict, rates: dict) -> float:
    """Tool dollars for one ticket = Σ(calls_for_tool × rate_for_tool) over the
    class's ACTUAL tool mix (refinement #3). At the default rates this equals
    the stored avg_tool_cost_usd exactly — tools absent from `rates`
    (task_classifier, merged spans) price at 0, as Phoenix counts them."""
    return sum((counts.get(t, 0) or 0) * r for t, r in rates.items())


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


def _issue_rows(live: dict, recs: list[dict], rates: dict):
    """Per-class derived view + page totals, recomputed from the live per-tool
    rate table. LLM cost is Phoenix-measured (fixed); the tool segment and the
    tool half of each saving move with the rates. At default rates this
    reproduces the stored numbers exactly. Returns (rows, totals)."""
    by = live.get("by_task_class") or {}
    total_n = int((live.get("summary") or {}).get("total_traces") or 0) or sum(s["n"] for s in by.values())
    cache_rec, routing = {}, None
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            cache_rec[i["task_class"]] = i
        elif i.get("kind") == "model_routing":
            routing = i
    base_s = by.get(BASELINE_CLASS) or {}
    base_cost = ((base_s.get("avg_llm_cost_usd", 0) or 0)
                 + _tool_cost(base_s.get("avg_tool_counts") or {}, rates)) or 1e-9
    rows, rec_tot, cost_tot = [], 0.0, 0.0
    for tc, s in by.items():
        n = s["n"]
        llm = s["avg_llm_cost_usd"]
        tool = _tool_cost(s.get("avg_tool_counts") or {}, rates)
        cost = llm + tool
        saving = 0.0
        ci = cache_rec.get(tc)
        if ci:
            comp = ci.get("components") or {}
            removed = comp.get("avg_tool_calls_removed", 0) or 0
            ts = removed * rates.get(ci.get("primary_tool"), 0.0)  # tool half scales with rate
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
    # Amber for cost-vs-baseline badges (refinement #5) — never red.
    bg, fg = ("#fdecd2", _AMBER) if danger else ("#e3f4e8", _GREEN)
    return (f"<span style='background:{bg};color:{fg};padding:1px 8px;border-radius:10px;"
            f"font-size:0.78rem;font-weight:700;white-space:nowrap'>{text}</span>")


def _bar(llm: float, tool: float, maxv: float) -> str:
    lw = (llm / maxv * 100) if maxv else 0
    tw = (tool / maxv * 100) if maxv else 0
    return (f"<div style='display:flex;height:13px;border-radius:4px;overflow:hidden;"
            f"background:#f0f0f0;margin:3px 0 6px;max-width:520px'>"
            f"<div title='tokens — measured by Phoenix' style='width:{lw:.2f}%;background:{_BLUE}'></div>"
            f"<div title='tools — call count × your rates' style='width:{tw:.2f}%;background:{_HATCH}'></div>"
            f"</div>")


def _class_reasons(recs: list[dict]) -> dict:
    out: dict = {}
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            comp = i.get("components") or {}
            n = round(comp.get("avg_tool_calls_removed", 0) or 0)
            out.setdefault(i["task_class"], []).append(
                f"repeats the same `{i.get('primary_tool', 'tool')}` ~{n}×")
        if i.get("kind") == "model_routing":
            for c in (i.get("classes") or []):
                out.setdefault(c, []).append("runs on the full-price model")
    return {tc: " · ".join(parts) for tc, parts in out.items()}


def _policy_mo(issue: dict, rates: dict, mt: int, total_n: int) -> float:
    n = issue.get("n_traces", 0) or 0
    if issue.get("kind") == "tool_cache":
        comp = issue.get("components") or {}
        spt = ((comp.get("avg_tool_calls_removed", 0) or 0) * rates.get(issue.get("primary_tool"), 0.0)
               + ((issue.get("savings_per_ticket_usd", 0) or 0)
                  - (comp.get("tool_savings_per_ticket_usd", 0) or 0)))
    else:
        spt = issue.get("savings_per_ticket_usd", 0) or 0
    return spt * mt * (n / total_n if total_n else 0)


def _policy_per_ticket(issue: dict, rates: dict) -> float:
    if issue.get("kind") == "tool_cache":
        comp = issue.get("components") or {}
        return ((comp.get("avg_tool_calls_removed", 0) or 0) * rates.get(issue.get("primary_tool"), 0.0)
                + ((issue.get("savings_per_ticket_usd", 0) or 0)
                   - (comp.get("tool_savings_per_ticket_usd", 0) or 0)))
    return issue.get("savings_per_ticket_usd", 0) or 0


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


# ===========================================================================
# CTO rebuild — the page is organized around MECHANISM and PROOF, not a dollar
# total. Regions: 1 identity/live-state · 2 trace race · 3 evidence at scale ·
# 4 diagnosis map · 5 governance control plane · 6 verification · 7 economics
# + roadmap footer. No large dollar figure opens the page.
# ===========================================================================

def _render_identity(live: dict, n_active: int, total_n: int) -> None:
    """Region 1 — what it is + that it's doing it now. Mechanism and aliveness,
    no dollar hero."""
    proj = os.environ.get("PHOENIX_PROJECT_NAME") or "your Phoenix"
    st.markdown(
        "<div style='font-size:1.7rem;font-weight:800;line-height:1.18'>"
        "An agent that governs another agent — at runtime.</div>"
        "<div style='color:#444;margin-top:4px;max-width:760px'>It reads the observed "
        "agent's traces from Phoenix, finds the calls it can avoid, and intercepts them "
        "inline at the gateway — <b>never touching your prompts or code</b>. Every claim "
        "below traces back to spans in your own Phoenix.</div>",
        unsafe_allow_html=True)
    live_dot = _GREEN if n_active > 0 else "#9ca3af"
    state = "Governing live" if n_active > 0 else "Watching — no policies active yet"
    c1, c2, c3 = st.columns([2, 1, 2])
    c1.markdown(f"<div style='font-size:1.05rem'><span style='color:{live_dot}'>●</span> "
                f"<b>{state}</b></div>", unsafe_allow_html=True)
    c2.markdown(f"<div style='font-size:1.05rem'><b>{n_active}</b> "
                f"polic{'y' if n_active == 1 else 'ies'} active</div>", unsafe_allow_html=True)
    c3.markdown(f"<div style='font-size:1.05rem;color:#444'>reading <b>{proj}</b> · "
                f"{total_n:,} traces analyzed</div>", unsafe_allow_html=True)


def _race_row_html(r: dict) -> str:
    op = r["op"]
    if r["status"] == "cached":
        gov = (f"<span style='color:{_GREEN};font-weight:600'>⊘ served from cache · $0</span>"
               f" <span style='color:#9ca3af'>(saved ${r['baseline']['cost']:.4f})</span>")
        base = f"<span style='color:#b45309'>${r['baseline']['cost']:.4f} paid</span>"
        bg = "#fbf6ee"
    elif r["status"] == "swapped":
        gov = (f"<span style='color:{_BLUE}'>→ {r['governed'].get('model') or 'cheaper model'} "
               f"· ${r['governed']['cost']:.4f}</span>")
        base = f"${r['baseline']['cost']:.4f}"
        bg = "#eef3fb"
    else:
        gov = f"<span style='color:#666'>${r['governed']['cost']:.4f}</span>"
        base = f"<span style='color:#666'>${r['baseline']['cost']:.4f}</span>"
        bg = "transparent"
    return (f"<tr style='background:{bg}'>"
            f"<td style='padding:3px 10px;font-family:monospace;font-size:0.82rem'>{op}</td>"
            f"<td style='padding:3px 10px;text-align:right;font-variant-numeric:tabular-nums'>{base}</td>"
            f"<td style='padding:3px 10px;font-variant-numeric:tabular-nums'>{gov}</td>"
            f"<td style='padding:3px 10px;text-align:right;color:#9ca3af;font-size:0.8rem'>"
            f"${r['base_cum']:.4f} / ${r['gov_cum']:.4f}</td></tr>")


def _render_trace_race() -> None:
    """Region 2 — the centerpiece. Renders the captured baseline-vs-governed
    race from the replay fixture: the legible row-by-row diff, the cost
    divergence, the cached calls, the same-answer proof. (Synchronized clock
    animation is layered on next; this is the already-aligned diff it plays.)"""
    from governor.trace_race.fixture import load_fixture
    st.subheader("The trace race — the same ticket, run two ways")
    fx = load_fixture()
    if not fx:
        st.info("**Seeded race pending.** Capture one with ingest on:\n\n"
                "`ACCOUNTANT_INGEST_URL=http://localhost:8765 uv run python -m "
                "governor.trace_race.capture \"<a refund ticket>\"`\n\n"
                "It runs the ticket policies-off (baseline) then policies-on (governed), "
                "captures both real traces, and drops them here.")
        return
    if not fx.get("seeded"):
        st.warning("Dev preview — built from two real refund traces that are **not the same "
                   "ticket**, so the same-answer proof is withheld. The shipped race uses a "
                   "same-ticket capture.")
    b, g = fx["baseline"], fx["governed"]
    st.caption(f"Ticket: {fx.get('ticket', '')[:140]}")
    rows = "".join(_race_row_html(r) for r in fx["rows"])
    st.markdown(
        f"<table style='width:100%;border-collapse:collapse;font-size:0.9rem'>"
        f"<thead><tr style='color:#6b7280;font-size:0.78rem;text-align:left'>"
        f"<th style='padding:2px 10px'>call</th>"
        f"<th style='padding:2px 10px;text-align:right'>baseline</th>"
        f"<th style='padding:2px 10px'>governed</th>"
        f"<th style='padding:2px 10px;text-align:right'>cumulative b / g</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>", unsafe_allow_html=True)
    cols = st.columns([1, 1, 1])
    cols[0].metric("Baseline cost", f"${b['total_usd']:.4f}")
    cols[1].metric("Governed cost", f"${g['total_usd']:.4f}",
                   delta=f"-${fx['saved_usd']:.4f}", delta_color="inverse")
    cols[2].metric("Paid calls skipped", f"{fx['skipped_calls']}")
    if fx.get("same_answer"):
        st.markdown(f"<span style='background:#e3f4e8;color:{_GREEN};padding:3px 10px;"
                    f"border-radius:10px;font-weight:700'>✓ Same final answer — verified "
                    f"identical on both lanes</span>", unsafe_allow_html=True)
    elif fx.get("seeded"):
        st.markdown(f"<span style='background:#fdecd2;color:{_AMBER};padding:3px 10px;"
                    f"border-radius:10px;font-weight:700'>⚠ Outputs differ — preservation "
                    f"not claimed</span>", unsafe_allow_html=True)
    lc = st.columns(2)
    if b.get("phoenix_url"):
        lc[0].link_button("Open baseline in Phoenix ↗", b["phoenix_url"], use_container_width=True)
    if g.get("phoenix_url"):
        lc[1].link_button("Open governed in Phoenix ↗", g["phoenix_url"], use_container_width=True)


def _render_evidence(gid) -> None:
    """Region 3 — verifiable evidence at scale. Verifiability is the feature."""
    st.subheader("Verifiable evidence — across all your traces")
    st.caption("Every figure here is re-derived from Phoenix-reconciled per-span data, and "
               "opens the underlying spans in your own Phoenix. Don't take our word for it.")
    _render_realized_savings()


def _render_governance(recs, default_mt, rates, total_n, gov_store) -> None:
    """Region 5 — the control plane: the levers + the guarantees that make
    flipping them safe (as prominent as the savings)."""
    st.subheader("Runtime governance — your control plane")
    st.markdown(
        f"<div style='background:#f6f8f4;border-left:3px solid {_GREEN};padding:8px 14px;"
        f"border-radius:4px;margin-bottom:8px'>"
        f"Intercepts at the gateway · <b>never changes your prompts or code</b> · "
        f"reversible in one click · quality-guarded with auto-rollback.</div>",
        unsafe_allow_html=True)
    _render_fixes(recs, default_mt, rates, total_n, gov_store)


def _render_economics_roadmap(recs, totals, default_mt) -> None:
    """Region 7 — subordinate footer. Money present but quiet; the waste essay
    collapses to one honest caveat; the Margin Agent is one roadmap line."""
    st.subheader("Economics & roadmap")
    cpt = float(totals["cost_per_ticket"])
    rpt = float(totals["recoverable_per_ticket"])
    vol = st.slider("Monthly volume (your number)", 1000,
                    max(100000, int(default_mt or 1000) * 3), int(default_mt or 1000),
                    1000, key="econ_vol")
    st.markdown(
        f"Measured **\\${cpt:.4f}/ticket**, of which **\\${rpt:.4f}/ticket** is avoidable → "
        f"projected **\\${rpt * vol:,.0f}/mo** recoverable at {vol:,} tickets.")
    st.caption("Per-ticket figures are **measured** from Phoenix; the monthly figure is a "
               "**projection** at the volume you set (upper bound — patterns overlap on shared "
               "ticket types).")
    st.caption("Roadmap — **Margin Agent**: turns this measured cost into credit pricing and "
               "per-segment margin defense. The CFO-facing extension.")


def _render_breakdown(rows, recs) -> None:
    """Merged 'where it goes' + 'what one ticket costs' (refinement #4). One
    row per PROBLEM class: badge + name + reason + a slim measured/estimated
    bar + a $/ticket caption. Baseline + minor classes collapse to one muted
    line. One legend, once (refinements #4, #5)."""
    st.subheader("Where the waste lives — and why")
    st.markdown(
        f"<span style='font-size:0.85rem;color:#555'>Per-ticket cost by task class, and the "
        f"pattern driving it. Split by certainty: "
        f"<span style='background:{_BLUE};color:#fff;padding:0 6px;border-radius:3px'>tokens — metered by Phoenix</span> "
        f"<span style='background:#e0a44a;color:#fff;padding:0 6px;border-radius:3px'>tools — call count × your rates</span>. "
        f"These patterns are what the policies below act on.</span>", unsafe_allow_html=True)
    reasons = _class_reasons(recs)
    maxv = max((r["cost"] for r in rows), default=1e-9)
    minor = []
    for r in rows:
        problem = (not r["is_base"]) and r["tc"] != "unknown" and r["mult"] >= 2.0
        if not problem:
            minor.append(r)
            continue
        badge = _badge(f"{r['share'] * 100:.0f}% · {r['mult']:.1f}× baseline", danger=True)
        why = reasons.get(r["tc"]) or "elevated vs baseline"
        st.markdown(
            f"{badge}&nbsp;&nbsp;**{r['tc'].replace('_', ' ').title()}** — {why}",
            unsafe_allow_html=True)
        st.markdown(_bar(r["llm"], r["tool"], maxv), unsafe_allow_html=True)
        st.markdown(
            f"<div style='color:#6b7280;font-size:0.82rem;margin:-2px 0 14px'>"
            f"<b>${r['cost']:.4f}/ticket</b> — ${r['llm']:.4f} tokens + ${r['tool']:.4f} tools"
            f"</div>", unsafe_allow_html=True)
    if minor:
        names = ", ".join(m["tc"].replace("_", " ") for m in minor if m["tc"] != "unknown")
        st.markdown(
            f"<div style='color:#9ca3af;font-size:0.86rem'>{names.capitalize()} are already "
            f"at baseline — nothing to fix.</div>", unsafe_allow_html=True)


def _render_fixes(recs, default_mt, rates, total_n, gov_store) -> None:
    """Promoted fix cards (refinement #1/#5). Each shows the measured per-ticket
    saving (the slider-independent atom) and its projection at the observed rate;
    the live what-if total lives in the hero. Activate is a calm neutral button,
    never alarm-red. (Header + safety guarantees are supplied by the governance
    region that wraps this.)"""
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
        mo = _policy_mo(issue, rates, default_mt, total_n)
        spt = _policy_per_ticket(issue, rates)
        total_proj += mo
        title, reason = _fix_text(issue)
        with st.container(border=True):
            c = st.columns([5, 2])
            with c[0]:
                st.markdown(f"### {title}")
                st.markdown(reason)
                tag = "✅ governing live · " if active else ""
                st.markdown(
                    f"<span style='color:{_GREEN};font-weight:700'>saves ${spt:.4f}/ticket</span> "
                    f"<span style='color:#888'>≈ ${mo:,.0f}/mo at the observed rate · "
                    f"{tag}runtime only, reversible</span>", unsafe_allow_html=True)
            with c[1]:
                if active:
                    if st.button("Turn off", key=f"deact_{sig}", use_container_width=True):
                        gov_store.deactivate_policy(sig)
                        st.rerun()
                else:
                    # Neutral (secondary) button — calm accent, not alarm red.
                    if st.button("Activate ↗", key=f"act_{sig}", use_container_width=True):
                        gov_store.activate_policy(sig, policy[1], policy[2])
                        st.rerun()
    st.markdown(
        f"<div style='display:flex;justify-content:space-between;align-items:center;"
        f"background:#f3f5ee;padding:10px 16px;border-radius:8px;margin-top:4px'>"
        f"<span style='font-weight:600'>Activate all {len(items)}</span>"
        f"<span style='color:{_GREEN};font-weight:800;font-size:1.3rem'>${total_proj:,.0f}/mo</span>"
        f"</div>", unsafe_allow_html=True)
    st.caption("Per-ticket savings are measured. The $/mo is that × the observed rate "
               "(~{:,}/mo); use the slider up top to project your own volume.".format(default_mt))


def _render_verification_layer(recs, gid, rates) -> None:
    """Region 6 — auditability as a feature: verify any number above against
    your own spans. (Realized savings moved up to region 3.)"""
    from governor.pipeline.db import savings_summary
    from governor.wrapper import store as gov_store
    saved = savings_summary()
    st.subheader("Verify any number against your own spans")
    st.caption("Trace IDs, Phoenix-measured LLM cost, counted tool calls, and the rates you set "
               "— and a link out to each trace. You set the tool prices; Phoenix counts the calls.")
    with st.expander("Open the audit surface — proof tables, tool rates, per-policy detail"):
        _render_tool_pricing(rates)
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

    from governor.wrapper import store as gov_store
    from governor.wrapper.store import active_policies
    from governor.pipeline.db import savings_summary

    recs = _load_recommendations()
    default_mt = _default_monthly_tickets(live, recs)
    # Per-tool rates live in session state, edited in the math drawer; every
    # bar and headline recomputes from them on the next rerun.
    rates = st.session_state.setdefault("tool_rates", _default_tool_rates())

    rows, totals = _issue_rows(live, recs, rates)
    n_active = len(active_policies())
    realized = savings_summary().get("total_savings_usd", 0) or 0
    gid = _project_gid()

    # CTO frame — mechanism and proof first; money subordinate. The trace race
    # is the visual center (region 2); no large dollar figure opens the page.
    _render_identity(live, n_active, totals["total_n"])           # R1
    st.divider()
    _render_trace_race()                                          # R2 (centerpiece)
    st.divider()
    _render_evidence(gid)                                         # R3
    st.divider()
    _render_breakdown(rows, recs)                                 # R4 (diagnosis map)
    st.divider()
    _render_governance(recs, default_mt, rates, totals["total_n"], gov_store)  # R5
    st.divider()
    _render_verification_layer(recs, gid, rates)                 # R6
    st.divider()
    _render_economics_roadmap(recs, totals, default_mt)          # R7


def main() -> None:
    st.title("Agent Accountant")
    st.caption("A runtime economic governor for AI agents — mechanism and proof, measured from Phoenix.")
    render_dashboard()


# ARCHIVED v5 (control-plane migration, Phase 1). Frozen reference for parity
# diffing. The only change from the live file is this __main__ guard, so the
# view-model functions can be imported for the Phase 2 parity check without
# launching the app. To run v5 visually, check out a pre-archive commit.
if __name__ == "__main__":
    main()
