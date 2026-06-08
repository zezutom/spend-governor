"""SQLite store for the Governor's real-time pipeline.

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
    "GOVERNOR_DB",
    str(Path(__file__).resolve().parents[3] / "data" / "accountant.db"),
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
    savings_usd REAL DEFAULT 0,
    cost_source TEXT DEFAULT 'local',
    reconciled_at TIMESTAMP,
    phoenix_node_id TEXT,
    annotated INTEGER NOT NULL DEFAULT 0,
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
    # Refactor #2: Phoenix-sourced cost reconciliation columns.
    if span_cols and "savings_usd" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN savings_usd REAL DEFAULT 0")
    if span_cols and "cost_source" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN cost_source TEXT DEFAULT 'local'")
    if span_cols and "reconciled_at" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN reconciled_at TIMESTAMP")
    if span_cols and "phoenix_node_id" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN phoenix_node_id TEXT")
    if span_cols and "annotated" not in span_cols:
        conn.execute("ALTER TABLE spans ADD COLUMN annotated INTEGER NOT NULL DEFAULT 0")


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


def update_phoenix_costs(rows: list[dict], conn=None) -> int:
    """Reconcile cost columns from Phoenix (refactor #2).

    Each row: {span_id, phoenix_cost_usd (None if Phoenix hasn't costed it
    yet, or a tool span Phoenix doesn't price), savings_usd}.
    - savings_usd is always written (from accountant.cost.savings_usd) so
      the headline 'Saved so far' is re-derivable from Phoenix.
    - llm_cost_usd is overwritten and cost_source flipped to 'phoenix' ONLY
      when Phoenix has a cost (LLM spans); otherwise the local-compute
      fallback stays in place (no blank cost during Phoenix's compute lag).
    Returns the number of cache rows actually updated.
    """
    def _run(c) -> int:
        n = 0
        for r in rows:
            pc = r.get("phoenix_cost_usd")
            sav = float(r.get("savings_usd") or 0.0)
            node = r.get("phoenix_node_id")
            if pc is not None:
                cur = c.execute(
                    "UPDATE spans SET llm_cost_usd=?, savings_usd=?, "
                    "phoenix_node_id=COALESCE(?, phoenix_node_id), "
                    "cost_source='phoenix', reconciled_at=CURRENT_TIMESTAMP "
                    "WHERE span_id=?",
                    (float(pc), sav, node, r["span_id"]),
                )
            else:
                cur = c.execute(
                    "UPDATE spans SET savings_usd=?, "
                    "phoenix_node_id=COALESCE(?, phoenix_node_id), "
                    "reconciled_at=CURRENT_TIMESTAMP WHERE span_id=?",
                    (sav, node, r["span_id"]),
                )
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return n
    if conn is None:
        with connect() as c:
            c.execute("BEGIN")
            n = _run(c)
            c.execute("COMMIT")
            return n
    return _run(conn)


def savings_summary(conn=None) -> dict:
    """Total realized savings from Phoenix-sourced per-span savings_usd
    (refactor #2 — replaces the private accountant_interventions log as the
    'Saved so far' source, so the number is re-derivable by a customer from
    their own Phoenix spans)."""
    def _run(c) -> dict:
        row = c.execute(
            "SELECT COALESCE(SUM(savings_usd),0) AS saved, "
            "SUM(CASE WHEN savings_usd>0 THEN 1 ELSE 0 END) AS n, "
            "SUM(CASE WHEN savings_usd>0 AND cache_hit=1 THEN 1 ELSE 0 END) AS cache_hits, "
            "SUM(CASE WHEN savings_usd>0 AND cache_hit=0 THEN 1 ELSE 0 END) AS model_swaps, "
            "COALESCE(SUM(CASE WHEN cache_hit=1 THEN savings_usd ELSE 0 END),0) AS cache_saved, "
            "COALESCE(SUM(CASE WHEN cache_hit=0 THEN savings_usd ELSE 0 END),0) AS model_saved, "
            "SUM(CASE WHEN cost_source='phoenix' THEN 1 ELSE 0 END) AS reconciled "
            "FROM spans"
        ).fetchone()
        return {
            "total_savings_usd": round(float(row["saved"] or 0), 6),
            "spans_with_savings": int(row["n"] or 0),
            "cache_hits": int(row["cache_hits"] or 0),
            "model_swaps": int(row["model_swaps"] or 0),
            "cache_savings_usd": round(float(row["cache_saved"] or 0), 6),
            "model_savings_usd": round(float(row["model_saved"] or 0), 6),
            "spans_reconciled": int(row["reconciled"] or 0),
        }
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def representative_saving_span(cache_hit: bool, conn=None) -> dict | None:
    """A recent saving span of a mechanism (cache_hit True ⇒ a served cache
    hit, False ⇒ a model downgrade) for the per-policy "verify in Phoenix"
    deeplink. Returns {trace_id, phoenix_node_id} or None."""
    def _run(c):
        r = c.execute(
            "SELECT trace_id, phoenix_node_id FROM spans "
            "WHERE savings_usd > 0 AND cache_hit = ? AND trace_id IS NOT NULL "
            "ORDER BY reconciled_at DESC, start_time DESC LIMIT 1",
            (1 if cache_hit else 0,),
        ).fetchone()
        return {"trace_id": r["trace_id"], "phoenix_node_id": r["phoenix_node_id"]} if r else None
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def class_cost_stats(task_classes: list[str], conn=None) -> dict:
    """Per-trace cost spread (n / min / avg / max) for one or more task
    classes — so the dashboard can show that a per-ticket waste figure is an
    AVERAGE with a real range, not a uniform number."""
    if not task_classes:
        return {"n": 0, "min": 0.0, "avg": 0.0, "max": 0.0}
    ph = ",".join("?" * len(task_classes))
    def _run(c) -> dict:
        row = c.execute(
            f"WITH tc AS (SELECT DISTINCT trace_id FROM spans "
            f"            WHERE classifier_task_class IN ({ph})), "
            f"pt AS (SELECT s.trace_id, SUM(s.llm_cost_usd + s.tool_cost_usd) cost "
            f"       FROM spans s JOIN tc ON s.trace_id = tc.trace_id "
            f"       GROUP BY s.trace_id) "
            f"SELECT COUNT(*) n, COALESCE(MIN(cost),0) mn, "
            f"COALESCE(AVG(cost),0) av, COALESCE(MAX(cost),0) mx FROM pt",
            task_classes,
        ).fetchone()
        return {"n": int(row["n"] or 0), "min": float(row["mn"] or 0),
                "avg": float(row["av"] or 0), "max": float(row["mx"] or 0)}
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def class_trace_costs(task_classes: list[str], limit: int = 20, offset: int = 0,
                      conn=None) -> list[dict]:
    """One page of traces for the given task class(es): trace_id + measured
    cost + web_search count — each row links to the trace in Phoenix so the
    per-ticket cost behind the waste figure is verifiable."""
    if not task_classes:
        return []
    ph = ",".join("?" * len(task_classes))
    def _run(c) -> list[dict]:
        rows = c.execute(
            f"WITH tc AS (SELECT DISTINCT trace_id FROM spans "
            f"            WHERE classifier_task_class IN ({ph})) "
            f"SELECT s.trace_id, "
            f"SUM(s.llm_cost_usd) llm_cost, SUM(s.tool_cost_usd) tool_cost, "
            f"SUM(CASE WHEN s.tool_name='web_search' THEN 1 ELSE 0 END) n_ws, "
            f"MIN(s.start_time) t "
            f"FROM spans s JOIN tc ON s.trace_id = tc.trace_id "
            f"GROUP BY s.trace_id ORDER BY t DESC LIMIT ? OFFSET ?",
            (*task_classes, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def policy_savings_series(cache_hit: bool, conn=None) -> list[dict]:
    """Per-span savings over time for a policy kind (cache_hit True ⇒ tool
    cache hits, False ⇒ model downgrades), oldest-first — feeds the
    cumulative-savings timeline chart."""
    def _run(c) -> list[dict]:
        rows = c.execute(
            "SELECT start_time, savings_usd FROM spans "
            "WHERE savings_usd > 0 AND cache_hit = ? AND start_time IS NOT NULL "
            "ORDER BY start_time",
            (1 if cache_hit else 0,),
        ).fetchall()
        return [{"start_time": r["start_time"], "savings_usd": float(r["savings_usd"] or 0)}
                for r in rows]
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def policy_saving_spans(cache_hit: bool, limit: int = 25, offset: int = 0,
                        conn=None) -> list[dict]:
    """One page of the saving spans for a policy kind, newest-first — each row
    carries the ids to deeplink into Phoenix (paginated drill-down)."""
    def _run(c) -> list[dict]:
        rows = c.execute(
            "SELECT span_id, trace_id, phoenix_node_id, savings_usd, start_time, "
            "tool_name, model_name FROM spans "
            "WHERE savings_usd > 0 AND cache_hit = ? "
            "ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (1 if cache_hit else 0, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def unannotated_saving_spans(limit: int = 500, conn=None) -> list[dict]:
    """Saving spans not yet tagged in Phoenix — fed to the annotator so each
    governed span gets an `accountant.savings` annotation exactly once."""
    def _run(c) -> list[dict]:
        rows = c.execute(
            "SELECT span_id, savings_usd, cache_hit FROM spans "
            "WHERE savings_usd > 0 AND annotated = 0 LIMIT ?",
            (limit,),
        ).fetchall()
        return [{
            "span_id": r["span_id"],
            "savings_usd": float(r["savings_usd"] or 0),
            "kind": "cache hit" if r["cache_hit"] else "model downgrade",
        } for r in rows]
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def mark_spans_annotated(span_ids: list[str], conn=None) -> None:
    """Flag spans as annotated so the reconcile loop doesn't re-tag them."""
    if not span_ids:
        return
    placeholders = ",".join("?" * len(span_ids))
    sql = f"UPDATE spans SET annotated = 1 WHERE span_id IN ({placeholders})"
    if conn is None:
        with connect() as c:
            c.execute(sql, span_ids)
    else:
        conn.execute(sql, span_ids)


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
