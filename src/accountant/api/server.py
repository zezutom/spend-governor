"""FastAPI cockpit API — a THIN layer over accountant.service (no logic moved,
no figure changes) + the SSE stream the agent narrates through.

The React cockpit consumes only this. Endpoints:
- GET  /api/state          current cockpit snapshot (canvas + counters)
- GET  /api/stream         SSE — agent activity + state, pushed on its own clock
- POST /api/action/{kind}/{sig}   a canvas turn: veto | enable | accept | reject
- POST /api/reset          restart the demo ungoverned
- GET  /api/proof          the captured before/after pair + Phoenix Cloud links

Run:  uv run uvicorn accountant.api.server:app --port 8800
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

load_dotenv()

from accountant import service
from accountant.api.governor import governor


@asynccontextmanager
async def lifespan(app: FastAPI):
    await governor.start()  # agent begins reasoning + auto-applying on its clock
    yield


app = FastAPI(title="Agent Accountant — Control Plane API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/state")
def get_state() -> dict:
    return governor.snapshot()


@app.get("/api/stream")
async def stream():
    """SSE: replays the current state immediately (first paint never waits on the
    agent), then streams every governor event."""
    q = governor.subscribe()

    async def gen():
        # immediate snapshot so the canvas paints at once
        yield {"event": "message",
               "data": json.dumps({"seq": 0, "narration": None, "state": governor.snapshot()})}
        try:
            while True:
                ev = await q.get()
                yield {"event": "message", "data": json.dumps(ev)}
        except asyncio.CancelledError:
            raise
        finally:
            governor.unsubscribe(q)

    return EventSourceResponse(gen())


@app.get("/api/ask")
async def ask(q: str | None = None, agent: str | None = None):
    """Live 'Ask the Accountant': runs the real ADK agent, which introspects its
    own Phoenix operational data at runtime via the Phoenix MCP server, and
    streams each tool-call + the answer (SSE). `agent` seeds a per-agent
    investigation; `q` is a free-form question."""
    from accountant.api import ask as ask_mod

    question, meta = q, {}
    if agent:
        a = next((x for x in governor.snapshot().get("agents", []) if x["id"] == agent), None)
        label = a["label"] if a else agent
        question = ask_mod.seed_question(agent, label, (a or {}).get("waste"))
        meta = {"agent": agent, "label": label}
    if not question:
        raise HTTPException(400, "provide ?q=<question> or ?agent=<id>")

    phoenix_base = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")

    async def gen():
        yield {"event": "message",
               "data": json.dumps({"type": "question", "question": question,
                                   "phoenix_base": phoenix_base, **meta})}
        async for step in ask_mod.ask_stream(question):
            yield {"event": "message", "data": json.dumps(step)}

    return EventSourceResponse(gen())


_ACTIONS = {"veto": "veto", "enable": "enable", "accept": "accept", "reject": "reject"}


@app.post("/api/action/{kind}/{sig:path}")
async def action(kind: str, sig: str) -> dict:
    if kind not in _ACTIONS:
        raise HTTPException(400, f"unknown action {kind}")
    await getattr(governor, _ACTIONS[kind])(sig)
    return {"ok": True, "state": governor.snapshot()}


@app.post("/api/reset")
async def reset() -> dict:
    await governor.reset()
    return {"ok": True, "state": governor.snapshot()}


@app.post("/api/clock/ff")
async def clock_ff(hours: float = 2.0) -> dict:
    """Advance the compressed scenario clock on cue (the presenter's fast-forward)."""
    await governor.fast_forward(hours)
    return {"ok": True, "state": governor.snapshot()}


@app.get("/api/series")
def series() -> dict:
    """The value-spine time-series: the compressed clock, the metrics history
    ($/message measured, quality eval-measured, volume seeded), the decision pins,
    and the closing summary. Cheap chart refetch (the same data also rides every
    snapshot on /api/stream)."""
    snap = governor.snapshot()
    return {k: snap.get(k) for k in ("clock", "history", "pins", "summary", "quality_basis",
                                     "baseline_dollars_per_message", "dollars_per_message")}


@app.get("/api/summary")
def summary() -> dict:
    """The closing takeaway: started at $X, now $Y (▼N%), quality held except the
    one dip you reverted, every step reversible — read straight off the series."""
    s = governor.snapshot().get("summary")
    if s is None:
        raise HTTPException(404, "no summary yet — the series is still warming up")
    return s


# Proof drill-down is scoped to a fleet agent (or a generic node).
from accountant.api.governor import _FLEET, _FLEET_ORDER  # noqa: E402

_KNOWN_CLASSES = set(_FLEET_ORDER)
_CLASS_TITLE = {aid: _FLEET[aid]["label"] for aid in _FLEET_ORDER}
_NODE_TITLE = {"tools": "Tool gateway", "model": "Model routing", "requests": "Incoming traffic"}


def _node_insight(node: str) -> dict:
    if node in _KNOWN_CLASSES:  # a fleet agent → that agent's traces
        classes = [node]
        title = _CLASS_TITLE[node]
        pair = service.captured_trace_pair() if _FLEET[node]["fix"] == "cache_tool" else None
    else:
        classes = list(_FLEET_ORDER)
        title = _NODE_TITLE.get(node, node)
        pair = service.captured_trace_pair() if node in ("tools", "gateway") else None
    gid = service.project_gid()
    rows = service.class_trace_costs(classes, 8, 0)
    traces = [{
        "trace_id": r["trace_id"],
        "llm_cost": r.get("llm_cost", 0) or 0,
        "tool_cost": r.get("tool_cost", 0) or 0,
        "total": (r.get("llm_cost", 0) or 0) + (r.get("tool_cost", 0) or 0),
        "phoenix_url": service.span_deeplink(gid, r["trace_id"], None),
    } for r in rows]
    return {"node": node, "title": title, "classes": classes,
            "pair": pair, "stats": service.class_cost_stats(classes), "traces": traces}


@app.get("/api/verify")
def verify() -> dict:
    """The Phoenix 'courtroom' view: the agent's last VERIFY result (the measured
    $/message delta, re-read from the same traffic) plus the captured before/after
    trace pair and its Phoenix Cloud deep-links. Real measurement, no scripted
    numbers; the same-answer claim is present only when the pair proves it."""
    v = governor.verify
    pair = service.captured_trace_pair()
    return {
        "verify": v,
        "pair": pair,
        "project_gid": service.project_gid(),
        "ready": v is not None,
    }


@app.get("/api/eval/{key}")
def eval_result(key: str) -> dict:
    """The accelerated quality eval, pre-run and cached. The model-eval popup
    reveals these REAL rows on the disclosed accelerated clock. Real replays
    through both models, real LLM-judge scores, real Phoenix trace links per
    row; the verdict is the agent's, and carries no Phoenix link."""
    from accountant.analytics import quality_eval
    r = quality_eval.load_eval(key)
    if r is None:
        raise HTTPException(404, f"no cached eval '{key}' — pre-run it first")
    return r


@app.get("/api/debug/{tc}")
def debug_box(tc: str) -> dict:
    """The debugger's per-box view: the cost of one workload broken to each call,
    with the SOURCE of every number — LLM measured by Phoenix (trace-linkable),
    tools at the operator's editable rates. Plus the real levers to take manual
    control. Cost uses the governor's live rates, so an edited rate is reflected."""
    rates = governor.rates()
    live = service.live_state()
    recs = service.recommendations()
    rows, _ = service.cost_breakdown(live, recs, rates)
    row = next((r for r in rows if r["tc"] == tc), None)
    if not row:
        raise HTTPException(404, f"no workload '{tc}'")
    by = live.get("by_task_class") or {}
    counts = (by.get(tc) or {}).get("avg_tool_counts") or {}
    llm = (by.get(tc) or {}).get("avg_llm_cost_usd", 0) or 0
    tool_rows = sorted(
        ({"tool": t, "count": round(counts.get(t, 0) or 0, 2), "rate": rate,
          "cost": round((counts.get(t, 0) or 0) * rate, 6)}
         for t, rate in rates.items() if (counts.get(t, 0) or 0) >= 0.05),
        key=lambda x: -x["cost"])
    gid = service.project_gid()
    tr = service.class_trace_costs([tc], 1, 0)
    llm_url = service.span_deeplink(gid, tr[0]["trace_id"], None) if tr else None
    pattern = (service.class_reasons(recs).get(tc) or "").strip()
    if not pattern and tool_rows:
        top = max(tool_rows, key=lambda x: x["count"])
        if top["count"] >= 1.5:
            pattern = f"Repeated {top['tool']} ×{round(top['count'])} on the premium model — same lookup more than once."
    active = {p["signature"] for p in service.active_policies()}
    # Each fleet agent has ONE fix. A SAFE fix (cache/cap) shows as a cache-style
    # control (force-on); a RISKY fix (suppress/route) shows as a route-style
    # control gated by its quick eval (None ⇒ evidence is the lab/proof).
    rt = governor.route_for_tc(tc)
    fix_label = _FLEET[tc]["fix_label"] if tc in _FLEET else None
    cache = route = None
    if rt and not rt["risky"]:
        cache = {"sig": rt["sig"], "active": rt["sig"] in active, "type": rt["type"], "label": fix_label}
    elif rt and rt["risky"]:
        route = {"risky": True, "sig": rt["sig"], "eval_key": rt["eval_key"], "type": rt["type"],
                 "use_case": tc, "label": fix_label, "active": rt["sig"] in active}
    return {
        "tc": tc, "title": _CLASS_TITLE.get(tc, tc.replace("_", " ")),
        "purpose": (_FLEET[tc]["purpose"] if tc in _FLEET else None),
        "share": round(row["share"], 3), "cost_per_message": round(row["cost"], 6),
        "llm_cost": round(llm, 6), "tool_cost": round(row["tool"], 6),
        "pattern": pattern, "llm_url": llm_url, "tool_rows": tool_rows,
        "cache": cache, "route": route,
    }


@app.post("/api/tool_rate")
async def tool_rate(tool: str, rate: float) -> dict:
    """Edit an operator tool rate; the governor recomputes cost everywhere."""
    await governor.set_tool_rate(tool, rate)
    return {"ok": True, "state": governor.snapshot()}


@app.get("/api/lab/{use_case}")
def lab_result(use_case: str) -> dict:
    """The replay-at-scale lab result, PRE-RUN and stored: N real past
    conversations replayed in a sandbox (spans tagged 'test', live untouched).
    Each row carries the premium AND economy model cost + per-tool cost/dup + both
    judge verdicts, so the UI derives any {cache, economy} config's impact from
    this one run. Augmented with monthly_volume for the production $ projection."""
    from accountant.analytics import quality_eval
    r = quality_eval.load_eval(f"lab_{use_case}")
    if r is None:
        raise HTTPException(404, f"lab '{use_case}' not pre-run")
    try:  # projection input: this use case's monthly message volume (operator volume × share)
        live = service.live_state(); recs = service.recommendations(); rates = service.default_tool_rates()
        rows, _ = service.cost_breakdown(live, recs, rates)
        share = next((x["share"] for x in rows if x["tc"] == use_case), 0.0)
        r["monthly_volume"] = int(round(governor.volume * share))
    except Exception:
        r["monthly_volume"] = None
    # The sandbox replay's spans are exported in a burst and dropped by Phoenix
    # Cloud's ingest limit, so per-replay deep-links 404. The REAL corpus spans
    # (generated paced → durable) DO resolve, so we link each tool call to a real
    # corpus span of this agent+tool (OTel ids the /redirects/spans route resolves).
    r["corpus_tool_spans"], r["corpus_trace_span"] = _corpus_span_ids(use_case)
    return r


def _corpus_span_ids(agent: str) -> tuple[dict, str | None]:
    """One representative DURABLE corpus span id per tool for an agent (+ a
    representative model span for the conversation-level link), read from the
    local span store. These are real spans that reliably resolve in Phoenix."""
    from accountant.pipeline import db
    tools: dict[str, str] = {}
    trace_span = None
    try:
        with db.connect() as con:
            sub = ("SELECT trace_id FROM spans WHERE tool_name='task_classifier' "
                   "AND classifier_task_class=?")
            for tool, sid in con.execute(
                    f"SELECT w.tool_name, MIN(w.span_id) FROM spans w "
                    f"JOIN ({sub}) c ON w.trace_id=c.trace_id "
                    f"WHERE w.tool_name IS NOT NULL AND w.tool_name<>'task_classifier' "
                    f"GROUP BY w.tool_name", (agent,)).fetchall():
                if sid:
                    tools[tool] = sid
            row = con.execute(
                f"SELECT m.span_id FROM spans m JOIN ({sub}) c ON m.trace_id=c.trace_id "
                f"WHERE m.span_kind='LLM' AND m.span_id IS NOT NULL LIMIT 1", (agent,)).fetchone()
            trace_span = row[0] if row else None
    except Exception:
        pass
    return tools, trace_span


@app.get("/api/lab/{use_case}/trickle")
async def lab_trickle(use_case: str, idx: int = 0) -> dict:
    """One REAL replay run live — the visible trickle so the pre-run batch
    doesn't feel canned. Sandbox + tagged 'test'; never touches live policies."""
    from accountant.analytics import quality_eval
    return await asyncio.to_thread(quality_eval.replay_one_live, use_case, idx=idx)


@app.get("/api/lab/{use_case}/run")
async def lab_run(use_case: str, n: int = 12, source: str = "replay"):
    """Execute a load test LIVE and stream each real replay as it lands (SSE), so
    the impact genuinely re-measures. n is clamped to a feasible live range; the
    displayed count = what actually ran. Sandbox, spans tagged 'test'."""
    import threading
    from accountant.analytics import quality_eval
    # replay is bounded by real history; synthetic can generate any volume (capped
    # only to keep a single live run sane — the narrative 'unlimited' is real, you
    # just wouldn't run thousands live on stage).
    n = max(1, min(int(n), 60 if source == "synthetic" else 24))
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def worker():
        try:
            for row in quality_eval.iter_lab_rows(use_case, n, source):
                loop.call_soon_threadsafe(q.put_nowait, {"row": row})
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(q.put_nowait, {"error": str(e)[:200]})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, {"done": True})

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            ev = await q.get()
            yield {"event": "message", "data": json.dumps(ev)}
            if ev.get("done"):
                break

    return EventSourceResponse(gen())


@app.post("/api/lab/apply")
async def lab_apply(use_case: str, cache: bool = False, economy: bool = False,
                    held_pct: float | None = None, degraded_pct: float | None = None,
                    saved_pct: float | None = None, projected_monthly: float | None = None,
                    source: str = "replay", n: int | None = None) -> dict:
    """Promote a debug-session config to production: the agent activates the chosen
    real levers, deactivates the rest, logs a session record, and writes the
    decision (+ advisory, + #DS link) to the inbox. The one sanctioned crossing."""
    res = await governor.apply_from_debug(use_case, cache, economy, evidence={
        "held_pct": held_pct, "degraded_pct": degraded_pct, "saved_pct": saved_pct,
        "projected_monthly": projected_monthly, "source": source, "n": n})
    return {"ok": True, "applied": res, "state": governor.snapshot()}


@app.get("/api/session/{sid}")
def session_record(sid: str) -> dict:
    """A debug session's record — for the inbox link's metadata popup. The agent's
    memory of a decision: levers, evidence, advice-against, watching status."""
    r = governor.sessions.get(sid)
    if r is None:
        raise HTTPException(404, f"no session {sid}")
    return r


@app.get("/api/proof")
def proof() -> dict:
    return _node_insight("tools")


@app.get("/api/proof/{node}")
def proof_node(node: str) -> dict:
    return _node_insight(node)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "project": os.environ.get("PHOENIX_PROJECT_NAME")}
