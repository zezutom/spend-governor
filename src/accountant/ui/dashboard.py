"""AI Cost Governance — the control-plane cockpit.

You don't read this page; you govern by talking to it. The optimizer reasons
over real Phoenix-reconciled cost (through the `accountant.service` interface —
the ONLY backend surface the UI touches), enacts real levers, and the system
map shows the consequence. Every figure is service-computed; nothing here is
fabricated, no roadmap lever is faked, and any claim is one click from the real
Phoenix spans.

    uv run streamlit run src/accountant/ui/dashboard.py
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from accountant import service
from accountant.optimizer import intents

INGEST_HOST = "127.0.0.1"
INGEST_PORT = int(os.environ.get("ACCOUNTANT_INGEST_PORT", "8765"))
INGEST_URL = f"http://{INGEST_HOST}:{INGEST_PORT}"
LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "ingest_server.log"

_GREEN, _BLUE, _AMBER, _DIM, _INK = "#15803d", "#2563eb", "#b45309", "#9ca3af", "#111827"

st.set_page_config(page_title="Agent Accountant — Control Plane", layout="wide")


# --- ingest bootstrap (unchanged infra) ------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port)); return True
        except OSError:
            return False


def _ensure_ingest_server() -> None:
    if st.session_state.get("ingest_server_checked"):
        return
    if _port_open(INGEST_HOST, INGEST_PORT):
        st.session_state["ingest_server_checked"] = True
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "accountant.pipeline.ingest_server:app",
         "--host", INGEST_HOST, "--port", str(INGEST_PORT), "--log-level", "info"],
        stdout=log_f, stderr=log_f, start_new_session=True)
    for _ in range(40):
        if _port_open(INGEST_HOST, INGEST_PORT):
            break
        time.sleep(0.25)
    st.session_state["ingest_server_checked"] = True


def _post_backfill_start() -> None:
    try:
        httpx.post(f"{INGEST_URL}/backfill/start", timeout=3.0)
    except Exception:
        pass


def _md(text: str) -> None:
    """Render markdown but neutralize Streamlit's $-as-LaTeX (cost strings)."""
    st.markdown(text.replace("$", "\\$"))


# --- onboarding (empty cache → live Phoenix import) ------------------------

@st.fragment(run_every="1.5s")
def _render_onboarding() -> None:
    live = service.live_state()
    ingest = live.get("ingest") or {}
    summary = live.get("summary") or {}
    if service.cache_span_count() > 0 and ingest.get("status") == "complete":
        st.rerun()
    _post_backfill_start()
    with st.container(border=True):
        st.markdown("### Connecting to Phoenix")
        st.caption("Importing trace history. The control plane comes online once cost data lands.")
        c1, c2 = st.columns(2)
        c1.metric("Traces imported", f"{int(summary.get('total_traces') or 0):,}")
        c2.metric("Spans", f"{int(summary.get('total_spans') or 0):,}")
        st.progress(min(max(float(ingest.get("progress") or 0.05), 0.0), 1.0),
                    text=ingest.get("message") or "Connecting…")


# --- the live system-behavior map ------------------------------------------

def _node(label: str, sub: str, on: bool, roadmap: bool = False) -> str:
    if roadmap:
        bd, fg, tag = "#d1d5db", _DIM, "roadmap"
    elif on:
        bd, fg, tag = _GREEN, _GREEN, "governed"
    else:
        bd, fg, tag = _AMBER, _AMBER, "paying"
    return (f"<div style='border:1.5px solid {bd};border-radius:8px;padding:8px 12px;"
            f"min-width:120px;background:#fff'>"
            f"<div style='font-weight:700;font-size:0.86rem;color:{_INK}'>{label}</div>"
            f"<div style='font-size:0.74rem;color:{fg}'>{sub}</div>"
            f"<div style='font-size:0.64rem;color:{fg};text-transform:uppercase;"
            f"letter-spacing:.04em'>{tag}</div></div>")


def _arrow(on: bool) -> str:
    c = _GREEN if on else _DIM
    return f"<div style='align-self:center;color:{c};font-size:1.1rem'>→</div>"


def _system_map() -> None:
    ws = service.is_active("cache_tool:web_search")
    kb = service.is_active("cache_tool:kb_lookup")
    rt = service.is_active("route_model:simple")
    nodes = "".join([
        _node("Incoming tickets", "request path", True),
        _arrow(True),
        _node("Router", "classify + dispatch", True),
        _arrow(True),
        _node("Tool gateway", "web_search · kb_lookup", ws or kb),
        _arrow(ws or kb),
        _node("Cache" if (ws or kb) else "External tool APIs",
              "semantic cache · $0" if (ws or kb) else "paid per call", ws or kb),
    ])
    models = "".join([
        _node("LLM router", "per task class", rt),
        _arrow(rt),
        _node("Economy model" if rt else "Premium model",
              "gemini-2.5-flash-lite" if rt else "full-price", rt),
        _arrow(False),
        _node("Budget controller", "spend caps · allocation", False, roadmap=True),
    ])
    html = (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif'>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px'>{nodes}</div>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap'>{models}</div></div>")
    st.html(html)


# --- conversation ----------------------------------------------------------

def _convo() -> list:
    return st.session_state.setdefault("convo", [])


def _run(handler, *, enact: bool = False, prompt: str | None = None) -> None:
    if prompt:
        _convo().append({"role": "user", "say": prompt})
    res = handler(enact=enact) if enact else handler()
    res["_at"] = time.strftime("%H:%M:%S")
    _convo().append(res)


def _freeform(text: str) -> None:
    """Q&A flourish: map a free question onto a curated intent by keyword. The
    reliable spine is the buttons; this never invents numbers (it routes to a
    deterministic handler)."""
    _convo().append({"role": "user", "say": text})
    t = text.lower()
    if any(w in t for w in ("why", "spike", "jump", "expensive", "high")):
        _run(intents.diagnose)
    elif any(w in t for w in ("cut", "reduce", "save", "lower")):
        _run(lambda enact=True: intents.cut_spend(0.30, enact=enact), enact=True)
    elif any(w in t for w in ("prevent", "stop", "fix", "again")):
        _run(lambda enact=True: intents.prevent(enact=enact), enact=True)
    elif any(w in t for w in ("prove", "real", "verify", "show")):
        _run(intents.prove)
    elif any(w in t for w in ("forecast", "month", "budget", "project")):
        _run(intents.forecast)
    else:
        _convo().append({"intent": "unknown", "_at": time.strftime("%H:%M:%S"),
                         "say": "Try: why costs spiked · cut spend · prevent recurrence · "
                                "prove it · forecast. (Curated intents are the reliable path.)"})


def _render_result(res: dict) -> None:
    if res.get("role") == "user":
        with st.chat_message("user"):
            st.markdown(res.get("say", ""))
        return
    with st.chat_message("assistant", avatar="🛰️"):
        if res.get("title"):
            st.markdown(f"**{res['title']}**")
        _md(res.get("say", ""))
        for card in res.get("cards", []):
            badge = (f"<span style='color:{_GREEN}'>● live</span>" if card.get("active")
                     else f"<span style='color:{_AMBER}'>○ proposed</span>")
            st.markdown(
                f"<div style='border:1px solid #e5e7eb;border-radius:6px;padding:6px 10px;"
                f"margin:3px 0'><b>{card['title']}</b> {badge}<br>"
                f"<span style='color:{_DIM};font-size:0.82rem'>saves "
                f"${card['per_ticket_usd']:.4f}/ticket · ~${card['monthly_usd']:,.0f}/mo "
                f"projected</span></div>", unsafe_allow_html=True)
        if res.get("proof"):
            _render_proof(res["proof"], res.get("deeplinks", {}))


# --- proof drill-down (system behaviour only) ------------------------------

def _render_proof(fx: dict, deeplinks: dict) -> None:
    rows = ""
    for r in fx["rows"]:
        if r["status"] == "cached":
            gov = f"<span style='color:{_GREEN}'>cached · $0</span>"
            base = f"<span style='color:{_AMBER}'>${r['baseline']['cost']:.4f}</span>"
            bg = "#fbf6ee"
        else:
            gov = f"<span style='color:#666'>${r['governed']['cost']:.4f}</span>"
            base = f"<span style='color:#666'>${r['baseline']['cost']:.4f}</span>"
            bg = "transparent"
        rows += (f"<tr style='background:{bg}'>"
                 f"<td style='padding:2px 8px;font-family:monospace;font-size:0.78rem'>{r['op']}</td>"
                 f"<td style='padding:2px 8px;text-align:right'>{base}</td>"
                 f"<td style='padding:2px 8px'>{gov}</td></tr>")
    st.markdown(
        f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem'>"
        f"<thead><tr style='color:{_DIM};font-size:0.74rem;text-align:left'>"
        f"<th style='padding:2px 8px'>call</th><th style='padding:2px 8px;text-align:right'>baseline</th>"
        f"<th style='padding:2px 8px'>governed</th></tr></thead><tbody>{rows}</tbody></table>",
        unsafe_allow_html=True)
    bcol, gcol = st.columns(2)
    if deeplinks.get("baseline"):
        bcol.link_button("Baseline in Phoenix ↗", deeplinks["baseline"], use_container_width=True)
    if deeplinks.get("governed"):
        gcol.link_button("Governed in Phoenix ↗", deeplinks["governed"], use_container_width=True)
    st.caption("System behaviour only — span names, call counts, cost. No prompt text or PII.")


# --- right rail: feed + forecast -------------------------------------------

def _render_feed() -> None:
    st.markdown("##### Intervention feed")
    saved = service.realized_savings()
    st.caption(f"Measured savings so far: ${saved.get('total_savings_usd', 0) or 0:.4f} · "
               f"{saved.get('spans_with_savings', 0)} interventions "
               f"({saved.get('cache_hits', 0)} cache hits · {saved.get('model_swaps', 0)} model swaps)")
    levers = service.levers()
    if not any(l["active"] for l in levers):
        st.info("No levers governing yet. Ask the optimizer to cut spend or prevent recurrence.")
    for l in levers:
        with st.container(border=True):
            c = st.columns([4, 1])
            with c[0]:
                state = (f"<span style='color:{_GREEN}'>● governing live</span>" if l["active"]
                         else f"<span style='color:{_DIM}'>○ available</span>")
                st.markdown(f"**{l['title']}** {state}", unsafe_allow_html=True)
                st.caption("Intercepts at the gateway · never changes prompts or code · reversible.")
            with c[1]:
                if l["active"]:
                    if st.button("Off", key=f"off_{l['signature']}", use_container_width=True):
                        service.deactivate_policy(l["signature"]); st.rerun()
                else:
                    if st.button("On", key=f"on_{l['signature']}", type="primary",
                                 use_container_width=True):
                        service.activate_policy(l["signature"], l["policy_type"], l["params"])
                        st.rerun()
    with st.expander("Roadmap — recommend-only, not enforced"):
        for c in service.roadmap_capabilities():
            st.markdown(f"**{c['title']}** — {c['blurb']}")


def _render_forecast() -> None:
    st.markdown("##### Forecast")
    default_vol = service.default_monthly_volume(service.live_state(), service.recommendations())
    vol = st.slider("Monthly tickets (your number)", 1000, max(100000, default_vol * 3),
                    default_vol, 1000, key="forecast_vol")
    f = intents.forecast(vol)["projection"]
    a, b = st.columns(2)
    a.metric("Projected spend", f"${f['monthly_spend_usd']:,.0f}/mo")
    b.metric("Avoidable", f"${f['monthly_recoverable_usd']:,.0f}/mo")
    st.caption("Per-ticket cost is measured from Phoenix; monthly figures are projections "
               "at the volume you set.")


# --- the cockpit -----------------------------------------------------------

def _status_bar() -> None:
    n = service.policies_active_count()
    live = service.live_state()
    total_n = int((live.get("summary") or {}).get("total_traces") or 0)
    proj = os.environ.get("PHOENIX_PROJECT_NAME") or "Phoenix"
    dot = _GREEN if n > 0 else _DIM
    state = "Governing live" if n > 0 else "Standing by"
    saved = service.realized_savings().get("total_savings_usd", 0) or 0
    cols = st.columns([2, 1, 1, 2])
    cols[0].markdown(f"<span style='color:{dot};font-size:1.3rem'>●</span> "
                     f"**{state}**", unsafe_allow_html=True)
    cols[1].markdown(f"**{n}** lever{'s' if n != 1 else ''} live")
    cols[2].markdown(f"**${saved:.4f}** saved")
    cols[3].markdown(f"<span style='color:{_DIM}'>reading {proj} · "
                     f"{total_n:,} traces</span>", unsafe_allow_html=True)


def render_cockpit() -> None:
    _status_bar()
    st.divider()
    left, right = st.columns([0.56, 0.44], gap="large")

    with left:
        st.markdown("#### Talk to the optimizer")
        st.caption("State intent in plain language. It reasons over real cost, enacts real "
                   "levers, and proves it. Curated intents are the reliable path.")
        cset = intents.CURATED
        bcols = st.columns(len(cset))
        for i, item in enumerate(cset):
            if bcols[i].button(item["label"], key=f"intent_{item['id']}", use_container_width=True):
                if item["id"] == "cut_spend":
                    _run(lambda enact=True: intents.cut_spend(0.30, enact=enact),
                         enact=True, prompt=item["label"])
                elif item["id"] == "prevent":
                    _run(lambda enact=True: intents.prevent(enact=enact),
                         enact=True, prompt=item["label"])
                else:
                    _run(item["handler"], prompt=item["label"])
                st.rerun()
        for res in _convo():
            _render_result(res)

    with right:
        st.markdown("##### Live system behaviour")
        _system_map()
        st.divider()
        _render_feed()
        st.divider()
        _render_forecast()

    # Chat input lives at the top level (Streamlit pins it to the bottom and
    # rejects it nested inside columns).
    if prompt := st.chat_input("Ask the optimizer…  e.g. why did costs spike?"):
        _freeform(prompt)
        st.rerun()


def main() -> None:
    st.title("AI Cost Governance — Control Plane")
    st.caption("Phoenix tells you what happened. This controls what happens next.")
    _ensure_ingest_server()
    if service.cache_span_count() == 0:
        _render_onboarding()
        return
    render_cockpit()


main()
