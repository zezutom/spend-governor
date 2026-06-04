"""AI Cost Governance — the control plane (autonomous agent + live canvas).

The Agent Inbox is a live activity stream the AGENT drives — its own reasoning
(pure LLM, no pre-canned text) and the levers it enacts ITSELF, on its own clock.
No approval gate: the agent detects each leak and calls activate_policy on its
own; the feed posts, the canvas reroutes, and the burn rate steps down while the
human does nothing. The human only SUPERVISES — every action is reversible
(one-click undo, which the agent treats as a correction and re-reasons over) and
"show me it's real" is optional inspection, never required to advance.

Bounds: only enactable levers are auto-enacted; roadmap items are surfaced
recommend-only and never enacted; the forward proposal tiers down to a
quality-floor guard. Every figure is the real measured delta from the service
layer; the agent's prose carries no numbers.

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
from accountant.optimizer import agent

INGEST_HOST = "127.0.0.1"
INGEST_PORT = int(os.environ.get("ACCOUNTANT_INGEST_PORT", "8765"))
INGEST_URL = f"http://{INGEST_HOST}:{INGEST_PORT}"
LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "ingest_server.log"

_GREEN, _AMBER, _DIM, _INK, _BG = "#0f6e56", "#85540b", "#9ca3af", "#141413", "#f5f4ed"
_MIN_PER_MONTH = 30 * 24 * 60
_TICK_SECONDS = 5.0
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


def _esc(text: str) -> str:
    return text.replace("$", "\\$")


@st.fragment(run_every="1.5s")
def _render_onboarding() -> None:
    live = service.live_state()
    ingest = live.get("ingest") or {}
    if service.cache_span_count() > 0 and ingest.get("status") == "complete":
        st.rerun()
    _post_backfill_start()
    with st.container(border=True):
        st.markdown("### Connecting to Phoenix")
        st.caption("Importing trace history. The control plane comes online once cost data lands.")
        st.progress(min(max(float(ingest.get("progress") or 0.05), 0.0), 1.0),
                    text=ingest.get("message") or "Connecting…")


# --- autonomous agent loop -------------------------------------------------

def _volume() -> int:
    return int(st.session_state.setdefault("volume", 4_000_000))


def _session_reset_if_new() -> None:
    """Each fresh browser session restarts the demo ungoverned, so it runs
    hands-off from zero. (DB lever state persists; this clears it for the demo.)"""
    if st.session_state.get("session_init"):
        return
    for p in service.active_policies():
        service.deactivate_policy(p["signature"])
    st.session_state.update(session_init=True, feed=[], vetoed=[], agent_error=None)
    st.session_state.pop("plan", None)
    st.session_state.pop("last_enact", None)  # clock starts after the agent reasons


def _ensure_plan() -> None:
    """One LLM reasoning cycle → the agent's observations + ordered plan. Called
    on first load and after a correction (a veto), never every tick — the timer
    enacts the existing plan; it does not re-call the model each step."""
    if "plan" in st.session_state:
        return
    try:
        dec = agent.decide(st.session_state.get("vetoed", []))
        st.session_state["plan"] = [{"lever": s.lever, "reason": s.reason} for s in dec.plan]
        st.session_state["observations"] = list(dec.observations)
        st.session_state["holding"] = dec.holding
        st.session_state["agent_error"] = None
    except Exception as e:  # quota/transient — degrade, don't crash the loop
        st.session_state.setdefault("plan", [])
        st.session_state.setdefault("observations", [])
        st.session_state["agent_error"] = str(e)[:160]


def _advance() -> None:
    """The autonomous beat: the agent enacts the next planned lever ITSELF and
    posts a card. One lever per tick. Roadmap is never enacted. Returns nothing;
    side effects are real (activate_policy) and the feed/canvas reflect them."""
    rates = service.default_tool_rates()
    live = service.live_state(); recs = service.recommendations()
    _, totals = service.cost_breakdown(live, recs, rates)
    mt = _volume()
    vetoed = set(st.session_state.get("vetoed", []))
    active = {p["signature"] for p in service.active_policies()}
    feed = st.session_state.setdefault("feed", [])
    by_sig = {l["signature"]: l for l in service.levers()}
    for step in st.session_state.get("plan", []):
        sig = step["lever"]
        lv = by_sig.get(sig)
        if sig in active or sig in vetoed or not lv or not lv["enactable"]:
            continue
        service.activate_policy(sig, lv["policy_type"], lv["params"])  # the agent pulls the trigger
        feed.insert(0, {
            "sig": sig, "title": lv["title"], "reason": step["reason"],
            "monthly": service.policy_monthly_saving(lv["issue"], rates, mt, totals["total_n"]),
            "node": _NODE_FOR.get(sig), "classes": lv["classes"],
        })
        return  # one enactment per tick


def _undo(sig: str) -> None:
    """A correction. Reverse the action AND veto it, so the autonomous agent
    respects the operator and re-reasons (re-plans) around the constraint."""
    service.deactivate_policy(sig)
    vetoed = st.session_state.setdefault("vetoed", [])
    if sig not in vetoed:
        vetoed.append(sig)
    st.session_state["feed"] = [c for c in st.session_state.get("feed", []) if c["sig"] != sig]
    st.session_state.pop("plan", None)  # force the agent to re-reason over the correction


def _next_focus() -> str | None:
    """The node the agent is about to act on — its current focus on the canvas."""
    vetoed = set(st.session_state.get("vetoed", []))
    active = {p["signature"] for p in service.active_policies()}
    for step in st.session_state.get("plan", []):
        if step["lever"] not in active and step["lever"] not in vetoed:
            return _NODE_FOR.get(step["lever"])
    return None


# --- the live canvas (self-animating SVG island) ---------------------------

def _burn() -> tuple[float, float]:
    rates = service.default_tool_rates()
    live = service.live_state(); recs = service.recommendations()
    _, totals = service.cost_breakdown(live, recs, rates)
    mt = _volume()
    gross = totals["cost_per_ticket"] * mt
    saved = sum(service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"])
                for l in service.levers() if l["active"] and l["enactable"])
    return max(gross - saved, 0.0) / _MIN_PER_MONTH, gross / _MIN_PER_MONTH


def _canvas(focus: str | None) -> None:
    ws = service.is_active("cache_tool:web_search")
    kb = service.is_active("cache_tool:kb_lookup")
    rt = service.is_active("route_model:simple")
    tools_gov = ws or kb
    burn_to, gross = _burn()
    burn_from = float(st.session_state.get("burn_prev", gross))
    st.session_state["burn_prev"] = burn_to

    nodes = {
        "requests": (24, 122, 92, 40, "Requests", "live traffic", None),
        "router": (150, 122, 78, 40, "Router", "classify", None),
        "gateway": (256, 70, 96, 30, "Tool gateway", "", None),
        "tools": (372, 64, 118, 40, "Cache" if tools_gov else "External tools",
                  "semantic · $0" if tools_gov else "paid per call", tools_gov),
        "model": (256, 188, 132, 42, "Economy model" if rt else "Premium model",
                  "flash-lite" if rt else "full-price", rt),
    }
    rects = ""
    for nid, (x, y, w, h, label, sub, gov) in nodes.items():
        stroke, fill, fg = (
            (_GREEN, "#e1f5ee", _GREEN) if gov is True else
            (_AMBER, "#faeeda", _AMBER) if gov is False else
            ("rgba(31,30,29,.3)", _BG, "#3d3d3a"))
        rects += (f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='8' fill='{fill}' "
                  f"stroke='{stroke}' stroke-width='1'/>"
                  f"<text x='{x+10}' y='{y+(15 if sub else h/2)}' font-size='12' font-weight='600' "
                  f"fill='{fg}'>{label}</text>"
                  + (f"<text x='{x+10}' y='{y+30}' font-size='10.5' fill='{fg}'>{sub}</text>" if sub else ""))
    edges = [
        {"p": [[116, 142], [150, 142]], "hot": False},
        {"p": [[228, 142], [256, 85]], "hot": False},
        {"p": [[352, 85], [372, 84]], "hot": not tools_gov},
        {"p": [[228, 142], [256, 209]], "hot": not rt},
    ]
    lines = "".join(f"<line x1='{e['p'][0][0]}' y1='{e['p'][0][1]}' x2='{e['p'][1][0]}' "
                    f"y2='{e['p'][1][1]}' stroke='{_AMBER if e['hot'] else '#c8c7c0'}' "
                    f"stroke-width='1.2'/>" for e in edges)
    ring = ""
    if focus in nodes:
        x, y, w, h, *_ = nodes[focus]
        ring = (f"<rect x='{x-5}' y='{y-5}' width='{w+10}' height='{h+10}' rx='11' fill='none' "
                f"stroke='{_AMBER}' stroke-width='1.6'><animate attributeName='opacity' "
                f"values='0.25;0.9;0.25' dur='1.4s' repeatCount='indefinite'/></rect>")

    cfg = json.dumps({"edges": edges, "burnFrom": burn_from, "burnTo": burn_to,
                      "down": burn_to < burn_from - 1e-9, "green": _GREEN, "amber": _AMBER})
    html = f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;position:relative">
  <svg id="cv" viewBox="0 0 520 240" width="100%" style="max-height:240px">{lines}{rects}{ring}</svg>
  <div style="position:absolute;top:2px;right:6px;text-align:right">
    <div style="font-size:11px;color:#6b6b66">burn rate</div>
    <div id="burn" style="font-size:18px;font-weight:700;color:{_INK}">$–/min</div>
    <div style="font-size:10px;color:{_DIM}">projected at {_volume():,}/mo</div>
  </div>
</div>
<script>
const C = {cfg}, NS = "http://www.w3.org/2000/svg", svg = document.getElementById("cv"), dots = [];
function mkDot(hot){{const c=document.createElementNS(NS,"circle");c.setAttribute("r",hot?3:2.4);
  c.setAttribute("fill",hot?C.amber:C.green);svg.appendChild(c);return c;}}
C.edges.forEach(e=>{{const n=e.hot?4:2,spd=e.hot?0.011:0.007;
  for(let k=0;k<n;k++)dots.push({{e,t:k/n,spd,el:mkDot(e.hot)}});}});
function frame(){{dots.forEach(d=>{{d.t+=d.spd;if(d.t>1)d.t-=1;const a=d.e.p[0],b=d.e.p[1];
  d.el.setAttribute("cx",a[0]+(b[0]-a[0])*d.t);d.el.setAttribute("cy",a[1]+(b[1]-a[1])*d.t);}});
  requestAnimationFrame(frame);}}
frame();
const burnEl=document.getElementById("burn");let t0=null;const dur=1500;
function fmt(v){{return "$"+(v<0.1?v.toFixed(4):v.toFixed(2))+"/min "+(C.down?"▼":"");}}
function bf(ts){{if(!t0)t0=ts;const k=Math.min((ts-t0)/dur,1);
  burnEl.textContent=fmt(C.burnFrom+(C.burnTo-C.burnFrom)*k);
  if(C.down)burnEl.style.color=C.green;if(k<1)requestAnimationFrame(bf);}}
requestAnimationFrame(bf);
</script>"""
    components.html(html, height=250)


# --- the agent inbox (activity stream the agent drives) --------------------

def _render_inbox() -> None:
    st.markdown("##### Agent inbox")
    st.markdown(f"<span style='color:{_GREEN};font-size:0.82rem'>● reasoning over live traffic"
                + ("  ·  ↻ recomputed after your correction" if st.session_state.get("vetoed") else "")
                + "</span>", unsafe_allow_html=True)
    if st.session_state.get("agent_error"):
        st.caption(f"agent paused (model): {st.session_state['agent_error']}")
    for o in st.session_state.get("observations", []):
        st.markdown(f"<span style='color:#3d3d3a;font-size:0.86rem'>· {o}</span>",
                    unsafe_allow_html=True)

    # actions the agent enacted itself — newest first
    for c in st.session_state.get("feed", []):
        with st.container(border=True):
            st.markdown(f"<span style='color:{_GREEN};font-weight:600'>✓ {c['title']}</span> "
                        f"<span style='color:{_GREEN};font-size:0.8rem'>· governing live · "
                        f"−${c['monthly']:,.0f}/mo</span>", unsafe_allow_html=True)
            st.markdown(f"<span style='color:#3d3d3a;font-size:0.82rem'>{c['reason']}</span>",
                        unsafe_allow_html=True)
            cols = st.columns(2)
            if cols[0].button("undo", key=f"undo_{c['sig']}", use_container_width=True):
                _undo(c["sig"]); st.rerun()
            if cols[1].button("show me it's real", key=f"insp_{c['sig']}", use_container_width=True):
                st.session_state["proof_open"] = True; st.rerun()

    # tiering down, honestly: once the agent has enacted what it can, it surfaces
    # the roadmap item recommend-only (never enacts it), then holds at the floor.
    active = {p["signature"] for p in service.active_policies()}
    vetoed = set(st.session_state.get("vetoed", []))
    remaining = [s for s in st.session_state.get("plan", [])
                 if s["lever"] not in active and s["lever"] not in vetoed]
    if not remaining and st.session_state.get("plan") is not None:
        for c in service.roadmap_capabilities()[:1]:
            with st.container(border=True):
                st.markdown(f"<span style='color:{_DIM}'>{c['title']} · <b>roadmap</b></span><br>"
                            f"<span style='color:{_DIM};font-size:0.78rem'>{c['blurb']} — "
                            f"recommend-only; the agent does not enact this.</span>",
                            unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown(f"<span style='color:{_DIM}'>Quality floor</span><br>"
                        f"<span style='color:{_DIM};font-size:0.78rem'>No further safe cut without "
                        f"risking answer quality. The agent holds here.</span>", unsafe_allow_html=True)


# --- proof drill-down (optional inspection, system behaviour only) ---------

def _render_proof() -> None:
    fx = service.captured_trace_pair()
    if not fx:
        st.info("No captured trace pair yet."); return
    b, g = fx["baseline"], fx["governed"]
    st.markdown(_esc(f"**Proof — same ticket, two ways.** baseline ${b['total_usd']:.4f} → "
                     f"governed ${g['total_usd']:.4f}, {fx['skipped_calls']} paid calls skipped, "
                     f"saved ${fx['saved_usd']:.4f}."))
    rows = ""
    for r in fx["rows"]:
        cached = r["status"] == "cached"
        gov = (f"<span style='color:{_GREEN}'>cached · $0</span>" if cached
               else f"<span style='color:#666'>${r['governed']['cost']:.4f}</span>")
        base = f"<span style='color:{_AMBER if cached else '#666'}'>${r['baseline']['cost']:.4f}</span>"
        rows += (f"<tr style='background:{'#faeeda' if cached else 'transparent'}'>"
                 f"<td style='padding:2px 8px;font-family:monospace;font-size:0.78rem'>{r['op']}</td>"
                 f"<td style='padding:2px 8px;text-align:right'>{base}</td>"
                 f"<td style='padding:2px 8px'>{gov}</td></tr>")
    st.markdown(f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem'><thead>"
                f"<tr style='color:{_DIM};font-size:0.74rem;text-align:left'><th style='padding:2px 8px'>call</th>"
                f"<th style='padding:2px 8px;text-align:right'>baseline</th><th style='padding:2px 8px'>governed</th>"
                f"</tr></thead><tbody>{rows}</tbody></table>", unsafe_allow_html=True)
    cc = st.columns(2)
    if b.get("phoenix_url"):
        cc[0].link_button("Baseline in Phoenix ↗", b["phoenix_url"], use_container_width=True)
    if g.get("phoenix_url"):
        cc[1].link_button("Governed in Phoenix ↗", g["phoenix_url"], use_container_width=True)
    st.caption("System behaviour only — span names, counts, cost. No prompt text or PII.")
    if st.button("close"):
        st.session_state["proof_open"] = False; st.rerun()


# --- the cockpit (runs hands-off on its own clock) -------------------------

@st.fragment(run_every="5s")
def render_cockpit() -> None:
    _session_reset_if_new()
    _ensure_plan()
    # Start the autonomous clock only AFTER the agent has reasoned, so the first
    # paint shows the ungoverned system; the agent then enacts a lever per tick.
    now = time.time()
    st.session_state.setdefault("last_enact", now)
    if now - st.session_state["last_enact"] >= _TICK_SECONDS - 0.5:
        _advance()
        st.session_state["last_enact"] = now

    n = service.policies_active_count()
    realized = service.realized_savings().get("total_savings_usd", 0) or 0
    top = st.columns([2, 2, 3])
    dot = _GREEN if n > 0 else _DIM
    top[0].markdown(f"<span style='color:{dot};font-size:1.2rem'>●</span> "
                    f"**{'Governing live' if n else 'Standing by'}** · {n} lever"
                    f"{'s' if n != 1 else ''}", unsafe_allow_html=True)
    top[1].markdown(f"<span style='color:{_DIM}'>historical measured: "
                    f"${realized:.4f} saved → inspect</span>", unsafe_allow_html=True)
    top[2].markdown(f"<div style='color:{_DIM};text-align:right'>autonomous · an agent governing "
                    f"another agent · reading {os.environ.get('PHOENIX_PROJECT_NAME','Phoenix')}</div>",
                    unsafe_allow_html=True)
    st.divider()

    left, right = st.columns([0.36, 0.64], gap="large")
    with left:
        _render_inbox()
    with right:
        st.markdown("##### Live system — the agent reroutes it in real time")
        _canvas(_next_focus())
        if st.session_state.get("proof_open"):
            st.divider()
            _render_proof()


def main() -> None:
    st.title("AI Cost Governance — Control Plane")
    st.caption("An agent that governs another agent's cost — autonomously. You supervise; you don't operate.")
    _ensure_ingest_server()
    if service.cache_span_count() == 0:
        _render_onboarding()
        return
    render_cockpit()


main()
