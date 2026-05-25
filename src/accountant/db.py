"""SQLite store for the Accountant's real-time pipeline.

Three tables matter:

- span_outbox: incoming span batches awaiting worker processing. Receiver
  inserts here and returns 200 immediately. Worker drains FIFO.
- spans: each span ingested, with cost already attached. Detection runs
  over this table.
- recommendations: current active recommendations (templated or Gemini).

WAL mode is enabled so the FastAPI receiver and the worker can both
write without blocking each other.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DB_PATH = os.environ.get(
    "ACCOUNTANT_DB",
    str(Path(__file__).resolve().parents[2] / "data" / "accountant.db"),
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS span_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    processed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON span_outbox(status, id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_id TEXT,
    span_kind TEXT,
    name TEXT,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    tool_name TEXT,
    classifier_task_class TEXT,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    cached_input_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    model_name TEXT,
    llm_cost_usd REAL DEFAULT 0,
    tool_cost_usd REAL DEFAULT 0,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_time ON spans(start_time);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(span_kind, trace_id);

CREATE TABLE IF NOT EXISTS recommendations (
    signature TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    task_class TEXT,
    anomaly_type TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    superseded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


_init_lock = threading.Lock()
_initialized: set[str] = set()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column
    existed. SQLite has no ADD COLUMN IF NOT EXISTS, so we check
    PRAGMA table_info first. Recommendations are derived/cheap to
    rebuild, so this only matters to avoid errors on an existing cache."""
    span_cols = {r[1] for r in conn.execute("PRAGMA table_info(spans)").fetchall()}
    if span_cols and "cache_hit" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0")


def _initialize(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def ensure_initialized(path: str = DEFAULT_DB_PATH) -> None:
    with _init_lock:
        if path in _initialized:
            return
        _initialize(path)
        _initialized.add(path)


@contextmanager
def connect(path: str = DEFAULT_DB_PATH):
    ensure_initialized(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def set_meta(key: str, value: str, conn=None) -> None:
    sql = (
        "INSERT INTO state_meta (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP"
    )
    if conn is None:
        with connect() as c:
            c.execute(sql, (key, value))
    else:
        conn.execute(sql, (key, value))


def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def enqueue_span_batch(payload_json: str, conn=None) -> int:
    """Insert a raw span batch payload into the outbox. Returns the new row id."""
    sql = "INSERT INTO span_outbox (payload) VALUES (?)"
    if conn is None:
        with connect() as c:
            cur = c.execute(sql, (payload_json,))
            return cur.lastrowid
    cur = conn.execute(sql, (payload_json,))
    return cur.lastrowid


def claim_pending_batches(limit: int = 50, conn=None) -> list[sqlite3.Row]:
    """Pop pending outbox rows for processing.

    Marks claimed rows as 'processing' atomically before returning, so a
    second worker (if ever added) won't pick them up.
    """
    sql_select = (
        "SELECT id, payload FROM span_outbox "
        "WHERE status = 'pending' ORDER BY id LIMIT ?"
    )
    if conn is None:
        with connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(sql_select, (limit,)).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                c.execute(
                    f"UPDATE span_outbox SET status='processing', attempts=attempts+1 "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
            c.execute("COMMIT")
            return rows
    c = conn
    c.execute("BEGIN IMMEDIATE")
    rows = c.execute(sql_select, (limit,)).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        c.execute(
            f"UPDATE span_outbox SET status='processing', attempts=attempts+1 "
            f"WHERE id IN ({placeholders})",
            ids,
        )
    c.execute("COMMIT")
    return rows


def mark_batch_processed(batch_id: int, conn=None) -> None:
    sql = (
        "UPDATE span_outbox SET status='processed', "
        "processed_at=CURRENT_TIMESTAMP WHERE id=?"
    )
    if conn is None:
        with connect() as c:
            c.execute(sql, (batch_id,))
    else:
        conn.execute(sql, (batch_id,))


def mark_batch_failed(batch_id: int, error: str, conn=None) -> None:
    sql = (
        "UPDATE span_outbox SET status='failed', last_error=? "
        "WHERE id=?"
    )
    if conn is None:
        with connect() as c:
            c.execute(sql, (error, batch_id))
    else:
        conn.execute(sql, (error, batch_id))


def upsert_span(span: dict, conn=None) -> None:
    """Insert a fully-costed span. Duplicate span_ids are ignored."""
    rec_defaults = {"cache_hit": 0, **span}
    span = rec_defaults
    sql = """
    INSERT INTO spans (
        span_id, trace_id, parent_id, span_kind, name,
        start_time, end_time, tool_name, classifier_task_class, cache_hit,
        prompt_tokens, cached_input_tokens, completion_tokens,
        reasoning_tokens, model_name, llm_cost_usd, tool_cost_usd
    ) VALUES (
        :span_id, :trace_id, :parent_id, :span_kind, :name,
        :start_time, :end_time, :tool_name, :classifier_task_class, :cache_hit,
        :prompt_tokens, :cached_input_tokens, :completion_tokens,
        :reasoning_tokens, :model_name, :llm_cost_usd, :tool_cost_usd
    )
    ON CONFLICT(span_id) DO NOTHING
    """
    if conn is None:
        with connect() as c:
            c.execute(sql, span)
    else:
        conn.execute(sql, span)


def upsert_recommendation(rec: dict, conn=None) -> None:
    sql = """
    INSERT INTO recommendations (
        signature, source, task_class, anomaly_type, title, description, data
    ) VALUES (
        :signature, :source, :task_class, :anomaly_type, :title, :description, :data
    )
    ON CONFLICT(signature) DO UPDATE SET
        source = excluded.source,
        title = excluded.title,
        description = excluded.description,
        data = excluded.data,
        updated_at = CURRENT_TIMESTAMP,
        superseded = 0
    """
    if conn is None:
        with connect() as c:
            c.execute(sql, rec)
    else:
        conn.execute(sql, rec)


def supersede_recommendations(active_signatures: set[str], conn=None) -> None:
    """Mark recommendations not in active_signatures as superseded, so the
    dashboard shows only currently-detected issues — if a pattern
    resolves, its card fades out instead of lingering."""
    if not active_signatures:
        return
    placeholders = ",".join("?" * len(active_signatures))
    sql = (
        f"UPDATE recommendations SET superseded=1 "
        f"WHERE signature NOT IN ({placeholders}) AND superseded=0"
    )
    if conn is None:
        with connect() as c:
            c.execute(sql, list(active_signatures))
    else:
        conn.execute(sql, list(active_signatures))
