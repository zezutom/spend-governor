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


def _render_hero(live: dict) -> None:
    """Value on the nose: lead with avoidable waste and realized savings,
    not trace counters. The two numbers a stressed CFO needs first."""
    from accountant.wrapper.store import active_policies

    summary = live.get("summary") or {}
    total_traces = int(summary.get("total_traces") or 0)
    total_cost = float(summary.get("total_cost_usd") or 0.0)
    last_updated = summary.get("last_updated_at") or "—"

    # Total avoidable waste = sum of every detected policy's monthly
    # opportunity. Realized = what the wrapper has actually saved so far —
    # re-derived from Phoenix-sourced per-span savings (refactor #2), so a
    # customer can verify it from their own traces.
    recs = _load_recommendations()
    opportunity = 0.0
    for r in recs:
        try:
            opportunity += float(json.loads(r.get("data") or "{}").get("monthly_savings_usd") or 0)
        except Exception:
            pass
    realized = float(summary.get("total_savings_usd") or 0.0)
    n_active = len(active_policies())

    c1, c2, c3 = st.columns(3)
    c1.metric(
        "💸 Avoidable AI waste",
        f"${opportunity:,.2f}/mo",
        help="Projected monthly cost of the wasteful execution patterns "
             "detected in your traces, at current volume.",
    )
    c2.metric(
        "✅ Saved so far",
        f"${realized:,.4f}",
        help="Actual cost the wrapper has avoided since you activated "
             "policies — summed from per-span savings in Phoenix, so it's "
             "verifiable from your own traces.",
    )
    c3.metric(
        "⚡ Policies governing live",
        f"{n_active}",
    )
    upd = last_updated.split("T")[1][:8] if "T" in last_updated else last_updated
    st.caption(
        f"{total_traces:,} tickets · ${total_cost:.2f} analyzed · updated {upd} UTC"
    )


BASELINE_CLASS = "password_reset"


def _render_by_class(live: dict) -> None:
    by_class = live.get("by_task_class") or {}
    if not by_class:
        st.info("Waiting for data…")
        return

    # Spend share per class — answers "where is the money going?"
    spend = {tc: (s["avg_cost_usd"] * s["n"]) for tc, s in by_class.items()}
    total_spend = sum(spend.values()) or 1e-9
    baseline = (by_class.get(BASELINE_CLASS) or {}).get("avg_cost_usd") or 1e-9

    ranked = sorted(by_class.items(), key=lambda kv: spend[kv[0]], reverse=True)

    # Signal-first headline: name the single biggest avoidable waste.
    worst_tc, worst_x = None, 1.0
    for tc, s in by_class.items():
        if tc in (BASELINE_CLASS, "unknown"):
            continue
        x = s["avg_cost_usd"] / baseline
        if x > worst_x:
            worst_tc, worst_x = tc, x
    if worst_tc:
        share = spend[worst_tc] / total_spend
        st.markdown(
            f"**{worst_tc.replace('_',' ').title()}** is your biggest avoidable "
            f"cost — **{worst_x:.1f}× the baseline** per ticket and "
            f"**{share*100:.0f}% of total spend**. See the policy below to fix it."
        )

    rows = []
    for tc, s in ranked:
        x = s["avg_cost_usd"] / baseline
        flag = "🟢 baseline" if tc == BASELINE_CLASS else (
            "🔴 wasteful" if x >= 2.0 else ("🟡 elevated" if x >= 1.5 else "🟢 ok")
        )
        rows.append({
            "Task type": tc.replace("_", " "),
            "Share of spend": f"{spend[tc]/total_spend*100:.0f}%",
            "Cost vs baseline": "—" if tc == BASELINE_CLASS else f"{x:.1f}×",
            "Status": flag,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with st.expander("Full per-class breakdown"):
        detail = []
        for tc, s in ranked:
            detail.append({
                "Task type": tc.replace("_", " "),
                "Tickets": s["n"],
                "Avg $/ticket": round(s["avg_cost_usd"], 5),
                "Avg LLM $": round(s["avg_llm_cost_usd"], 5),
                "Avg tool $": round(s["avg_tool_cost_usd"], 5),
                "Avg tools/ticket": s["avg_tools"],
                "Avg web_search/ticket": s["avg_web_search"],
            })
        st.dataframe(pd.DataFrame(detail), hide_index=True, use_container_width=True)


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


def _policy_explainer(issue: dict) -> dict:
    """Plain-English answers to the buyer's questions: what does it do,
    is it safe, what's the action labelled."""
    kind = issue.get("kind")
    if kind == "model_routing":
        cheap = issue.get("cheap_model", "a cheaper model")
        return {
            "what": (
                f"Routes **simple tickets** (password resets, account questions) "
                f"to **{cheap}** instead of the standard model — at the gateway, "
                f"as requests pass through."
            ),
            "safeguard": (
                "Only low-risk request types are routed. Refunds, plan changes, "
                "and anything involving money or a decision stay on the standard "
                "model — they're never downgraded."
            ),
            "button": "Activate routing",
        }
    tool = issue.get("primary_tool") or "the tool"
    return {
        "what": (
            f"Serves **repeated `{tool}` calls** from a cache instead of "
            f"re-running them — so the same external lookup isn't paid for "
            f"twice across tickets."
        ),
        "safeguard": (
            "A call is only served from cache when its query is **semantically "
            "equivalent** to a prior one (embedding check). A genuinely "
            "different query still runs — so answers don't degrade."
        ),
        "button": "Activate caching",
    }


def _render_savings_math(issue: dict) -> None:
    comp = issue.get("components") or {}
    st.markdown("**How this is calculated**")
    if issue.get("kind") == "model_routing":
        st.markdown(
            f"- {issue['n_traces']} simple tickets currently run on the standard model\n"
            f"- Routing to `{comp.get('cheap_model')}` retains ~"
            f"{int(comp.get('llm_cost_retained_ratio', 0)*100)}% of LLM cost "
            f"(estimated, input-heavy tasks)\n"
            f"- Saving per ticket: **${issue.get('savings_per_ticket_usd', 0):.4f}** "
            f"(${issue.get('current_avg_usd', 0):.4f} → ${issue.get('projected_avg_usd', 0):.4f})\n"
            f"- × {issue.get('monthly_volume', 0):,} tickets/month "
            f"(observed over {comp.get('window_days', 0):g} days) = "
            f"**${issue.get('monthly_savings_usd', 0):,.2f}/month**"
        )
        return
    removed = comp.get("avg_tool_calls_removed", 0)
    tool = issue.get("primary_tool") or "tool"
    unit = comp.get("tool_unit_price_usd", 0)
    st.markdown(
        f"- Serves ~{removed:g} repeated `{tool}` calls/ticket from semantic cache "
        f"× ${unit:.4f} = **${comp.get('tool_savings_per_ticket_usd', 0):.4f}** tool cost\n"
        f"- Plus the LLM reasoning those calls triggered: "
        f"**${comp.get('llm_savings_per_ticket_usd', 0):.4f}**\n"
        f"- Saving per ticket: **${issue.get('savings_per_ticket_usd', 0):.4f}** "
        f"(${issue.get('current_avg_usd', 0):.4f} → ${issue.get('projected_avg_usd', 0):.4f})\n"
        f"- × {issue.get('monthly_volume', 0):,} tickets/month "
        f"(observed over {comp.get('window_days', 0):g} days) = "
        f"**${issue.get('monthly_savings_usd', 0):,.2f}/month**"
    )


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
        f"**${m['before_avg_usd']:.4f} → ${m['after_avg_usd']:.4f}** "
        f"(**−{pct}%**), **${m['measured_savings_usd']:,.4f}** saved so far."
    )


def _render_recommendations(recs: list[dict]) -> None:
    from accountant.wrapper import store as gov_store

    st.markdown("#### Realized savings")
    _render_realized_savings()
    st.divider()
    st.markdown("#### Optimization policies")

    if not recs:
        st.success("No cost issues detected — the agent is running clean.")
        return

    for rec in recs:
        issue = _issue_of(rec)
        policy = _policy_for_issue(issue)
        badge = "🤖 reasoned" if rec["source"] == "gemini" else "📋 pattern"
        sig = policy[0] if policy else rec["signature"]
        active = gov_store.is_active(sig) if policy else False

        pct = int(round(issue.get("pct_reduction", 0) * 100))
        monthly = issue.get("monthly_savings_usd", 0) or 0
        cur = issue.get("current_avg_usd", 0) or 0
        proj = issue.get("projected_avg_usd", 0) or 0
        volume = issue.get("monthly_volume", 0) or 0
        has_savings = (issue.get("savings_per_ticket_usd", 0) or 0) > 0

        explain = _policy_explainer(issue) if policy else None

        with st.container(border=True):
            head = st.columns([5, 2])
            with head[0]:
                tag = "  ·  ✅ governing live" if active else ""
                st.markdown(f"**{rec['title']}**  ·  _{badge}_{tag}")
            with head[1]:
                if policy:
                    if active:
                        if st.button("Turn off", key=f"deact_{sig}",
                                     use_container_width=True,
                                     help="Stop enforcing this policy. Takes effect immediately."):
                            gov_store.deactivate_policy(sig)
                            st.rerun()
                    else:
                        if st.button(explain["button"], key=f"act_{sig}",
                                     type="primary", use_container_width=True,
                                     help="Enforced at the gateway in real time. "
                                          "Reversible in one click."):
                            gov_store.activate_policy(sig, policy[1], policy[2])
                            st.rerun()

            if has_savings:
                m1, m2 = st.columns(2)
                m1.metric("Saving per ticket", f"{pct}%")
                m2.metric("Saving per month", f"${monthly:,.2f}")
                st.caption(
                    f"${cur:.4f} → ${proj:.4f} per ticket  ·  "
                    f"~{volume:,} tickets/month at current volume"
                )

            # The buyer's three questions, answered before they click.
            if explain:
                st.markdown(f"**What it does** — {explain['what']}")
                st.markdown(f"**Safeguard** — {explain['safeguard']}")
                st.caption(
                    "🔒 Runtime only — never changes your prompts or code. "
                    "Reversible in one click."
                    + ("  ·  ✅ currently governing live." if active else "")
                )

            # Proof, once active: measured from the customer's own traces.
            if active and policy:
                _render_verification(issue, sig)

            with st.expander("The math"):
                if has_savings:
                    _render_savings_math(issue)
                else:
                    st.caption("No quantified savings for this item yet.")


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

    # Banner policy: while a backfill is running OR the cache is empty,
    # show the big onboarding banner ONLY — no header, no by-class
    # table. After backfill completes, the banner disappears and the
    # regular dashboard takes over. The same `live` blob feeds both
    # modes, so no cadence divergence is possible.
    show_onboarding = (
        span_count == 0
        or backfill_status in ("in_progress", "starting", None)
    ) and backfill_status != "complete"

    if show_onboarding:
        _render_onboarding(live)
        return

    _render_hero(live)
    st.divider()
    st.subheader("Where the money goes")
    _render_by_class(live)


def _is_onboarding() -> bool:
    live = _load_live_state()
    status = (live.get("ingest") or {}).get("status")
    return (
        _cache_span_count() == 0
        or status in ("in_progress", "starting", None)
    ) and status != "complete"


# Recommendations live in a separate, slower fragment. Two reasons:
# (1) the Apply/Revert buttons are interactive — a 500ms auto-rerun
# can race a click; 3s is gentle enough that clicks land reliably.
# (2) recommendations change on the order of seconds (when Gemini
# reasons), not per-span, so they don't need the fast cadence.
@st.fragment(run_every="3s")
def render_recommendations_section() -> None:
    if _is_onboarding():
        return
    st.divider()
    st.subheader("Recommendations")
    _render_recommendations(_load_recommendations())


def main() -> None:
    st.title("Agent Accountant")
    st.caption(
        "Live unit economics for the observed agent. "
        "Counters update in real time as new spans arrive."
    )
    render_live()
    render_recommendations_section()


main()
