"""AI Cost Governance — the control-plane cockpit (Agent Inbox + Live Canvas).

An ambient agent reasons over live traffic and ranks proposals in an inbox; the
canvas shows the consequence as motion (continuous traffic, a moving burn rate);
you approve; the canvas reroutes, the burn bends down, and the inbox recomputes
and re-ranks from REAL figures, jumping the highlight to the next leak's node.

Everything is read through the `accountant.service` interface only — no number
is invented in the UI, only real (enactable) levers are approvable, roadmap
items are recommend-only, and any governing item drills to the real Phoenix
spans. The canvas animates client-side (its own JS clock), so it is alive
before and between actions — not a static dashboard relabeled "live".

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
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

from accountant import service
from accountant.optimizer import intents

INGEST_HOST = "127.0.0.1"
INGEST_PORT = int(os.environ.get("ACCOUNTANT_INGEST_PORT", "8765"))
INGEST_URL = f"http://{INGEST_HOST}:{INGEST_PORT}"
LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "ingest_server.log"

_GREEN, _AMBER, _DIM, _INK, _BG = "#0f6e56", "#85540b", "#9ca3af", "#141413", "#f5f4ed"
_MIN_PER_MONTH = 30 * 24 * 60
# Which canvas node each lever targets (the on-node link to the inbox).
_NODE_FOR = {"cache_tool:web_search": "tools", "cache_tool:kb_lookup": "tools",
             "route_model:simple": "model"}

st.set_page_config(page_title="Agent Accountant — Control Plane", layout="wide")


# --- ingest bootstrap ------------------------------------------------------

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
    st.markdown(text.replace("$", "\\$"))


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
        st.progress(min(max(float(ingest.get("progress") or 0.05), 0.0), 1.0),
                    text=ingest.get("message") or "Connecting…")


# --- the agent's state: proposals, ranking, burn (all from service) --------

def _volume() -> int:
    live = service.live_state()
    return int(st.session_state.setdefault("volume", 4_000_000))


def _proposals():
    """Build the ranked inbox + burn from real figures. The queue is enactable
    levers not yet active, ranked by recomputed $/mo; the top is active. Approved
    levers settle to governing. Then labeled roadmap, then a quality-floor guard
    — the forward proposal always exists but never implies infinite savings."""
    live = service.live_state()
    recs = service.recommendations()
    rates = service.default_tool_rates()
    rows, totals = service.cost_breakdown(live, recs, rates)
    mt = _volume()
    total_n = totals["total_n"]

    def monthly(l):
        return service.policy_monthly_saving(l["issue"], rates, mt, total_n)

    enactable = [l for l in service.levers() if l["enactable"]]
    governing = sorted([l for l in enactable if l["active"]], key=monthly, reverse=True)
    queue = sorted([l for l in enactable if not l["active"]], key=monthly, reverse=True)

    def prop(l, state):
        return {"key": l["signature"], "title": l["title"], "cause": l["cause"],
                "per_ticket": service.policy_per_ticket_saving(l["issue"], rates),
                "monthly": monthly(l), "node": _NODE_FOR.get(l["signature"]),
                "state": state, "policy_type": l["policy_type"], "params": l["params"],
                "classes": l["classes"], "lever": l}

    active = prop(queue[0], "active") if queue else None
    queued = [prop(l, "queued") for l in queue[1:]]
    gov = [prop(l, "governing") for l in governing]

    gross = totals["cost_per_ticket"] * mt
    saved = sum(monthly(l) for l in governing)
    burn_to = max(gross - saved, 0.0) / _MIN_PER_MONTH

    return {"active": active, "queued": queued, "governing": gov,
            "roadmap": service.roadmap_capabilities(),
            "burn_to": burn_to, "gross_burn": gross / _MIN_PER_MONTH,
            "mt": mt, "totals": totals}


# --- the live canvas (self-animating SVG island) ---------------------------

def _canvas(state: dict, just_changed: bool) -> None:
    ws = service.is_active("cache_tool:web_search")
    kb = service.is_active("cache_tool:kb_lookup")
    rt = service.is_active("route_model:simple")
    tools_gov = ws or kb
    burn_to = state["burn_to"]
    burn_from = float(st.session_state.get("burn_prev", state["gross_burn"]))
    st.session_state["burn_prev"] = burn_to
    highlight = (state["active"] or {}).get("node")

    # nodes: id -> (x,y,w,h,label,sub,governed)
    nodes = {
        "requests": (24, 122, 92, 40, "Requests", "live traffic", None),
        "router": (150, 122, 78, 40, "Router", "classify", None),
        "tools": (372, 64, 118, 40, "Cache" if tools_gov else "External tools",
                  "semantic · $0" if tools_gov else "paid per call", tools_gov),
        "model": (256, 188, 132, 42, "Economy model" if rt else "Premium model",
                  "flash-lite" if rt else "full-price", rt),
        "gateway": (256, 70, 96, 30, "Tool gateway", "", None),
    }
    rects = ""
    for nid, (x, y, w, h, label, sub, gov) in nodes.items():
        if gov is True:
            stroke, fill, fg = _GREEN, "#e1f5ee", _GREEN
        elif gov is False:
            stroke, fill, fg = _AMBER, "#faeeda", _AMBER
        else:
            stroke, fill, fg = "rgba(31,30,29,.3)", _BG, "#3d3d3a"
        rects += (
            f"<rect id='node-{nid}' x='{x}' y='{y}' width='{w}' height='{h}' rx='8' "
            f"fill='{fill}' stroke='{stroke}' stroke-width='1'/>"
            f"<text x='{x + 10}' y='{y + (15 if sub else h/2)}' font-size='12' font-weight='600' "
            f"fill='{fg}'>{label}</text>"
            + (f"<text x='{x + 10}' y='{y + 30}' font-size='10.5' fill='{fg}'>{sub}</text>" if sub else ""))

    # edges: polyline [from-center -> to-center], hot = ungoverned paid path
    edges = [
        {"p": [[116, 142], [150, 142]], "hot": False, "on": True},          # requests→router
        {"p": [[228, 142], [256, 85]], "hot": False, "on": True},           # router→gateway
        {"p": [[352, 85], [372, 84]], "hot": not tools_gov, "on": True},    # gateway→tools
        {"p": [[228, 142], [256, 209]], "hot": not rt, "on": True},         # router→model
    ]
    lines = "".join(
        f"<line x1='{e['p'][0][0]}' y1='{e['p'][0][1]}' x2='{e['p'][1][0]}' y2='{e['p'][1][1]}' "
        f"stroke='{_AMBER if e['hot'] else '#c8c7c0'}' stroke-width='1.2'/>" for e in edges)

    ring = ""
    if highlight and highlight in nodes:
        x, y, w, h, *_ = nodes[highlight]
        ring = (f"<rect id='ring' x='{x-5}' y='{y-5}' width='{w+10}' height='{h+10}' rx='11' "
                f"fill='none' stroke='{_AMBER}' stroke-width='1.6'>"
                f"<animate attributeName='opacity' values='0.25;0.9;0.25' dur='1.6s' "
                f"repeatCount='indefinite'/></rect>")

    cfg = json.dumps({"edges": edges, "burnFrom": burn_from, "burnTo": burn_to,
                      "down": burn_to < burn_from - 1e-9, "changed": just_changed,
                      "green": _GREEN, "amber": _AMBER})
    html = f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;position:relative">
  <svg id="cv" viewBox="0 0 520 240" width="100%" style="max-height:240px">
    {lines}{rects}{ring}
  </svg>
  <div style="position:absolute;top:2px;right:6px;text-align:right">
    <div style="font-size:11px;color:#6b6b66">burn rate</div>
    <div id="burn" style="font-size:18px;font-weight:700;color:{_INK}">$–/min</div>
    <div style="font-size:10px;color:{_DIM}">projected at {state['mt']:,}/mo</div>
  </div>
</div>
<script>
const C = {cfg};
const NS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("cv");
const dots = [];
function mkDot(hot) {{
  const c = document.createElementNS(NS, "circle");
  c.setAttribute("r", hot ? 3 : 2.4);
  c.setAttribute("fill", hot ? C.amber : C.green);
  svg.appendChild(c); return c;
}}
C.edges.forEach(e => {{
  if (!e.on) return;
  const n = e.hot ? 4 : 2, spd = e.hot ? 0.011 : 0.007;
  for (let k = 0; k < n; k++) dots.push({{e, t: k / n, spd, el: mkDot(e.hot)}});
}});
function frame() {{
  dots.forEach(d => {{
    d.t += d.spd; if (d.t > 1) d.t -= 1;
    const a = d.e.p[0], b = d.e.p[1];
    d.el.setAttribute("cx", a[0] + (b[0] - a[0]) * d.t);
    d.el.setAttribute("cy", a[1] + (b[1] - a[1]) * d.t);
  }});
  requestAnimationFrame(frame);
}}
frame();
const burnEl = document.getElementById("burn");
const dur = C.changed ? 1500 : 600; let t0 = null;
function fmt(v) {{ return "$" + (v < 0.1 ? v.toFixed(4) : v.toFixed(2)) + "/min " + (C.down ? "▼" : ""); }}
function burnFrame(ts) {{
  if (!t0) t0 = ts; const k = Math.min((ts - t0) / dur, 1);
  burnEl.textContent = fmt(C.burnFrom + (C.burnTo - C.burnFrom) * k);
  if (C.down) burnEl.style.color = C.green;
  if (k < 1) requestAnimationFrame(burnFrame);
}}
requestAnimationFrame(burnFrame);
</script>"""
    components.html(html, height=250)


# --- the agent inbox -------------------------------------------------------

def _approve(p: dict) -> None:
    service.activate_policy(p["key"], p["policy_type"], p["params"])  # hard-guarded
    st.session_state["just_changed"] = True


def _inbox_card(p: dict, *, approvable: bool) -> None:
    if p["state"] == "governing":
        bd, fg, fill = _GREEN, _GREEN, "#e1f5ee"
    elif p["state"] == "active":
        bd, fg, fill = _AMBER, _AMBER, "#faeeda"
    else:
        bd, fg, fill = "rgba(31,30,29,.3)", "#3d3d3a", _BG
    with st.container(border=True):
        if p["state"] == "governing":
            st.markdown(f"<span style='color:{_GREEN};font-weight:600'>✓ {p['title']}</span><br>"
                        f"<span style='color:{_GREEN};font-size:0.8rem'>governing live · "
                        f"−${p['monthly']:,.0f}/mo</span>", unsafe_allow_html=True)
            if st.button("show me it's real", key=f"proof_{p['key']}", use_container_width=True):
                st.session_state["proof_open"] = True
                st.rerun()
        elif p["state"] == "active":
            st.markdown(f"<span style='color:{_AMBER};font-weight:700'>● {p['title']}</span><br>"
                        f"<span style='color:{_AMBER};font-size:0.8rem'>now top leak · "
                        f"saves ${p['per_ticket']:.4f}/ticket (~${p['monthly']:,.0f}/mo)</span>",
                        unsafe_allow_html=True)
            st.caption(p["cause"])
            if st.button("Approve →", key=f"appr_{p['key']}", type="primary",
                         use_container_width=True):
                _approve(p); st.rerun()
        else:  # queued
            st.markdown(f"**{p['title']}** <span style='color:{_DIM};font-size:0.8rem'>queued · "
                        f"~${p['monthly']:,.0f}/mo</span>", unsafe_allow_html=True)


def _render_inbox(state: dict) -> None:
    st.markdown("##### Agent inbox")
    st.markdown(f"<span style='color:{_GREEN};font-size:0.8rem'>● reasoning over live traffic"
                + ("  ·  ↻ recomputed after approve" if st.session_state.get("just_changed") else "")
                + "</span>", unsafe_allow_html=True)
    for p in state["governing"]:
        _inbox_card(p, approvable=False)
    if state["active"]:
        _inbox_card(state["active"], approvable=True)
    for p in state["queued"]:
        _inbox_card(p, approvable=False)
    # roadmap — recommend-only, not approvable
    for i, c in enumerate(state["roadmap"][:1]):
        with st.container(border=True):
            st.markdown(f"<span style='color:{_DIM}'>{c['title']} · "
                        f"<b>roadmap</b></span><br><span style='color:{_DIM};font-size:0.78rem'>"
                        f"{c['blurb']} — recommend-only, not enforced</span>", unsafe_allow_html=True)
    # quality-floor guard — the forward proposal never invents another cut
    if not state["active"]:
        with st.container(border=True):
            st.markdown(f"<span style='color:{_DIM}'>Quality floor reached</span><br>"
                        f"<span style='color:{_DIM};font-size:0.78rem'>No further safe cut without "
                        f"risking answer quality. The agent holds here.</span>", unsafe_allow_html=True)


# --- proof drill-down (system behaviour only) ------------------------------

def _render_proof() -> None:
    fx = service.captured_trace_pair()
    if not fx:
        st.info("No captured trace pair yet.")
        return
    b, g = fx["baseline"], fx["governed"]
    st.markdown(f"**Proof — same ticket, two ways.** baseline ${b['total_usd']:.4f} → governed "
                f"${g['total_usd']:.4f}, {fx['skipped_calls']} paid calls skipped, saved "
                f"${fx['saved_usd']:.4f}.".replace("$", "\\$"))
    rows = ""
    for r in fx["rows"]:
        cached = r["status"] == "cached"
        gov = (f"<span style='color:{_GREEN}'>cached · $0</span>" if cached
               else f"<span style='color:#666'>${r['governed']['cost']:.4f}</span>")
        base = (f"<span style='color:{_AMBER}'>${r['baseline']['cost']:.4f}</span>" if cached
                else f"<span style='color:#666'>${r['baseline']['cost']:.4f}</span>")
        rows += (f"<tr style='background:{'#faeeda' if cached else 'transparent'}'>"
                 f"<td style='padding:2px 8px;font-family:monospace;font-size:0.78rem'>{r['op']}</td>"
                 f"<td style='padding:2px 8px;text-align:right'>{base}</td>"
                 f"<td style='padding:2px 8px'>{gov}</td></tr>")
    st.markdown(f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem'><thead>"
                f"<tr style='color:{_DIM};font-size:0.74rem;text-align:left'><th style='padding:2px 8px'>call</th>"
                f"<th style='padding:2px 8px;text-align:right'>baseline</th><th style='padding:2px 8px'>governed</th>"
                f"</tr></thead><tbody>{rows}</tbody></table>", unsafe_allow_html=True)
    c = st.columns(2)
    if b.get("phoenix_url"):
        c[0].link_button("Baseline in Phoenix ↗", b["phoenix_url"], use_container_width=True)
    if g.get("phoenix_url"):
        c[1].link_button("Governed in Phoenix ↗", g["phoenix_url"], use_container_width=True)
    st.caption("System behaviour only — span names, counts, cost. No prompt text or PII.")


# --- secondary intent surface (supporting, not the hero) -------------------

def _render_intent_bar(state: dict) -> None:
    with st.expander("Or state intent in plain language (supporting surface)"):
        cols = st.columns(3)
        if cols[0].button("Cut spend 30%", use_container_width=True):
            r = intents.cut_spend(0.30, enact=True); st.session_state["just_changed"] = True
            st.session_state["last_intent"] = r["say"]; st.rerun()
        if cols[1].button("Prevent this again", use_container_width=True):
            if state["active"]:
                _approve(state["active"]); st.rerun()
        if cols[2].button("Show me it's real", use_container_width=True):
            st.session_state["proof_open"] = True; st.rerun()
        if st.session_state.get("last_intent"):
            _md(st.session_state["last_intent"])
        if prompt := st.chat_input("Ask the optimizer… (Q&A flourish)"):
            st.session_state["last_intent"] = _freeform_answer(prompt)
            st.rerun()


def _freeform_answer(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("why", "spike", "expensive", "high")):
        return intents.diagnose()["say"]
    if any(w in t for w in ("prove", "real", "verify")):
        st.session_state["proof_open"] = True
        return intents.prove()["say"]
    if any(w in t for w in ("forecast", "month", "budget")):
        return intents.forecast(_volume())["say"]
    return "Try: why costs spiked · prevent this · show me it's real · forecast."


# --- the cockpit -----------------------------------------------------------

def render_cockpit() -> None:
    state = _proposals()
    just_changed = bool(st.session_state.pop("just_changed", False))

    n = service.policies_active_count()
    realized = service.realized_savings().get("total_savings_usd", 0) or 0
    top = st.columns([2, 2, 3])
    dot = _GREEN if n > 0 else _DIM
    top[0].markdown(f"<span style='color:{dot};font-size:1.2rem'>●</span> "
                    f"**{'Governing live' if n else 'Standing by'}** · {n} lever"
                    f"{'s' if n != 1 else ''}", unsafe_allow_html=True)
    top[1].markdown(f"<span style='color:{_DIM}'>historical measured: "
                    f"${realized:.4f} saved → see proof</span>", unsafe_allow_html=True)
    top[2].markdown(f"<span style='color:{_DIM};text-align:right;display:block'>"
                    f"agent governing another agent at runtime · reading "
                    f"{os.environ.get('PHOENIX_PROJECT_NAME','Phoenix')}</span>",
                    unsafe_allow_html=True)
    st.divider()

    left, right = st.columns([0.34, 0.66], gap="large")
    with left:
        _render_inbox(state)
    with right:
        st.markdown("##### Live system — real traffic, governed in place")
        _canvas(state, just_changed)
        _render_intent_bar(state)
        if st.session_state.get("proof_open"):
            st.divider()
            _render_proof()
            if st.button("close proof"):
                st.session_state["proof_open"] = False; st.rerun()


def main() -> None:
    st.title("AI Cost Governance — Control Plane")
    st.caption("Phoenix tells you what happened. This controls what happens next.")
    _ensure_ingest_server()
    if service.cache_span_count() == 0:
        _render_onboarding()
        return
    render_cockpit()


main()
