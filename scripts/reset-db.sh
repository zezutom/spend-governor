#!/usr/bin/env bash
#
# Reset the Governor cache so the next launch is treated as a NEW
# account (triggers the Phoenix onboarding backfill).
#
#   1. Stops the dashboard (:8501) and ingest server (:8765).
#   2. Backs up the SQLite DB (timestamped) and removes it + WAL/SHM.
#
# The backup means an undo is instant instead of a slow Phoenix
# re-import. Relaunch yourself afterwards to watch the backfill live:
#
#   uv run streamlit run src/governor/ui/dashboard.py
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${GOVERNOR_DB:-$REPO_ROOT/data/accountant.db}"

# 1. Stop the app.
PIDS="$(lsof -ti :8765 -i :8501 2>/dev/null | sort -u || true)"
if [ -n "$PIDS" ]; then
  echo "Stopping app (PIDs: $(echo "$PIDS" | tr '\n' ' '))"
  echo "$PIDS" | xargs kill 2>/dev/null || true
  sleep 1
else
  echo "No process on :8765 or :8501"
fi

# 2. Back up and remove the DB (+ WAL/SHM sidecars).
if [ -f "$DB" ]; then
  BAK="$DB.$(date +%Y%m%d-%H%M%S).bak"
  mv "$DB" "$BAK"
  rm -f "$DB-wal" "$DB-shm"
  echo "Backed up cache → $BAK"
  echo "Cache reset. Undo with:  mv \"$BAK\" \"$DB\""
else
  echo "No DB at $DB — already empty (will onboard as a new account)."
fi

echo "Relaunch:  uv run streamlit run src/governor/ui/dashboard.py"
