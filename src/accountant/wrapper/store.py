"""Shared store for the wrapper's policies and interventions.

Lives in the same SQLite file as the rest of Agent Accountant (the demo's
shared store) and owns its policy + intervention tables.

- accountant_policies: operator-activated policies. The dashboard writes
  them; the wrapper reads the active ones at runtime.
- accountant_interventions: an append-only log of every real-time action
  the wrapper took (cache hit served, model downgraded) with the cost
  avoided. The dashboard aggregates these into the live savings number.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# This module lives at src/accountant/wrapper/store.py, so the repo root
# (which holds data/accountant.db) is four parents up.
DB_PATH = os.environ.get(
    "ACCOUNTANT_DB",
    str(Path(__file__).resolve().parents[3] / "data" / "accountant.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accountant_policies (
    signature TEXT PRIMARY KEY,
    policy_type TEXT NOT NULL,
    params TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accountant_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL,
    tool TEXT,
    task_class TEXT,
    detail TEXT,
    cost_avoided_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_interventions_ts ON accountant_interventions(ts);
"""

_lock = threading.Lock()
_ready = False


def _init() -> None:
    global _ready
    with _lock:
        if _ready:
            return
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as c:
            c.execute("PRAGMA journal_mode=WAL;")
            c.executescript(SCHEMA)
        _ready = True


@contextmanager
def connect():
    _init()
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# -- Policies ---------------------------------------------------------------

def activate_policy(signature: str, policy_type: str, params: dict) -> None:
    # Store the activation time as an explicit ISO-UTC string so the
    # before/after verification can compare it against span start_times
    # (also ISO-UTC) without timezone-format ambiguity.
    now = datetime.now(timezone.utc).isoformat()
    with connect() as c:
        c.execute(
            "INSERT INTO accountant_policies (signature, policy_type, params, active, updated_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(signature) DO UPDATE SET "
            "policy_type=excluded.policy_type, params=excluded.params, "
            "active=1, updated_at=excluded.updated_at",
            (signature, policy_type, json.dumps(params), now),
        )


def policy_activated_at(signature: str) -> str | None:
    with connect() as c:
        row = c.execute(
            "SELECT updated_at FROM accountant_policies WHERE signature=? AND active=1",
            (signature,),
        ).fetchone()
    return row["updated_at"] if row else None


def deactivate_policy(signature: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE accountant_policies SET active=0, updated_at=CURRENT_TIMESTAMP "
            "WHERE signature=?",
            (signature,),
        )


def active_policies() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT signature, policy_type, params FROM accountant_policies WHERE active=1"
        ).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["params"] or "{}")
        except Exception:
            params = {}
        out.append({"signature": r["signature"], "policy_type": r["policy_type"], "params": params})
    return out


def is_active(signature: str) -> bool:
    with connect() as c:
        row = c.execute(
            "SELECT active FROM accountant_policies WHERE signature=?", (signature,)
        ).fetchone()
    return bool(row and row["active"])


# -- Interventions ----------------------------------------------------------

def record_intervention(
    kind: str,
    tool: str | None,
    task_class: str | None,
    cost_avoided_usd: float,
    detail: dict | None = None,
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO accountant_interventions (kind, tool, task_class, detail, cost_avoided_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, tool, task_class, json.dumps(detail or {}), float(cost_avoided_usd)),
        )


def intervention_summary() -> dict:
    with connect() as c:
        total = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_avoided_usd),0) AS saved "
            "FROM accountant_interventions"
        ).fetchone()
        by_kind = c.execute(
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(cost_avoided_usd),0) AS saved "
            "FROM accountant_interventions GROUP BY kind"
        ).fetchall()
    return {
        "total_interventions": int(total["n"] or 0),
        "total_cost_avoided_usd": round(float(total["saved"] or 0), 6),
        "by_kind": {r["kind"]: {"n": int(r["n"]), "saved": round(float(r["saved"]), 6)} for r in by_kind},
    }
