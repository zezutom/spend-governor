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


# Each canvas node maps to the task classes that flow through it — so its proof
# is scoped to THAT node, not one global pair.
_NODE_CLASSES = {
    "tools": ["refund_handling", "account_question"],
    "gateway": ["refund_handling", "account_question"],
    "model": ["password_reset", "account_question"],
    "router": ["refund_handling", "account_question", "password_reset", "plan_change"],
    "requests": ["refund_handling", "account_question", "password_reset", "plan_change"],
}
_NODE_TITLE = {"tools": "Cache / tool gateway", "gateway": "Tool gateway",
               "model": "Model routing", "router": "Router", "requests": "Incoming requests"}


_KNOWN_CLASSES = {"refund_handling", "account_question", "password_reset", "plan_change"}
_CLASS_TITLE = {"refund_handling": "Refund tickets", "account_question": "Account questions",
                "password_reset": "Password resets", "plan_change": "Plan changes"}


def _node_insight(node: str) -> dict:
    if node in _KNOWN_CLASSES:  # a workload lane → that conversation type's traces
        classes = [node]
        title = _CLASS_TITLE[node]
        pair = service.captured_trace_pair() if node == "refund_handling" else None
    else:
        classes = _NODE_CLASSES.get(node, _NODE_CLASSES["requests"])
        title = _NODE_TITLE.get(node, node)
        # The captured before/after pair is a CACHING proof — tool/cache nodes only.
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


@app.get("/api/proof")
def proof() -> dict:
    return _node_insight("tools")


@app.get("/api/proof/{node}")
def proof_node(node: str) -> dict:
    return _node_insight(node)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "project": os.environ.get("PHOENIX_PROJECT_NAME")}
