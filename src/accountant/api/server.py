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


@app.get("/api/proof")
def proof() -> dict:
    fx = service.captured_trace_pair()
    if not fx:
        raise HTTPException(404, "no captured trace pair")
    # system behaviour only — already PII-free; pass through
    return fx


@app.get("/health")
def health() -> dict:
    return {"ok": True, "project": os.environ.get("PHOENIX_PROJECT_NAME")}
