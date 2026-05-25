"""FastAPI ingest server for the Accountant.

Fast-path handler: on POST /ingest, the body is inserted as one
transactional row into span_outbox and we return 200 immediately. No
cost computation, no detection, no I/O beyond the SQLite write. The
worker (running as an asyncio task in the same process) drains the
outbox in the background.

Run with:
    uv run uvicorn accountant.ingest_server:app --port 8000

The observed agent's accountant_exporter posts to /ingest when
ACCOUNTANT_INGEST_URL is set in its environment.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Response

from accountant import backfill, worker
from accountant.db import enqueue_span_batch, ensure_initialized, get_meta


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_initialized()
    worker_task = asyncio.create_task(worker.run_forever())
    # Refresh recommendations from an already-populated cache on boot,
    # so a restart (e.g. after a code change) reflects the current
    # analysis without waiting for new traffic.
    asyncio.create_task(worker.initial_refresh())
    log.info("worker task started")
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


@app.post("/ingest")
async def ingest(req: Request) -> Response:
    body = await req.body()
    if not body:
        return Response(content='{"status":"empty"}', media_type="application/json")
    enqueue_span_batch(body.decode("utf-8"))
    return Response(content='{"status":"ok"}', media_type="application/json")


@app.post("/backfill/start")
async def start_backfill() -> dict:
    """Kick off a Phoenix backfill if no task is currently running.

    Idempotent — calling repeatedly while a backfill is in progress
    returns {"status": "already_running"} without spawning another.
    Progress is reported via state_meta.backfill_state, which the
    dashboard reads on each auto-refresh.
    """
    return backfill.start_backfill_if_idle()


@app.get("/health")
async def health() -> dict:
    import json as _json
    backfill_raw = get_meta("backfill_state")
    backfill_state = None
    if backfill_raw:
        try:
            backfill_state = _json.loads(backfill_raw)
        except Exception:
            backfill_state = {"raw": backfill_raw}
    return {
        "status": "ok",
        "last_aggregation": get_meta("last_aggregation"),
        "total_traces": get_meta("total_traces"),
        "total_cost_usd": get_meta("total_cost_usd"),
        "backfill": backfill_state,
    }
