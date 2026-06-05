#!/usr/bin/env bash
# Start the AI Cost Governance control-plane cockpit:
#   - FastAPI cockpit API  (:8800)  — thin layer over accountant.service + SSE
#   - ingest server        (:8765)  — receives streamed traffic into the cache
#   - React dashboard (Vite, :5173) — the cockpit UI  ← open this
#
# Usage:   ./scripts/start-cockpit.sh
# Stop:    Ctrl-C (stops all three)
set -uo pipefail
cd "$(dirname "$0")/.."

API_PORT=8800
INGEST_PORT=8765
UI_PORT=5173

echo "→ stopping any existing instances…"
pkill -f "uvicorn accountant.api" 2>/dev/null || true
pkill -f "ingest_server:app" 2>/dev/null || true
pkill -f "vite" 2>/dev/null || true
sleep 1

if [ ! -d web/node_modules ]; then
  echo "→ installing dashboard deps (first run only)…"
  (cd web && npm install --no-fund --no-audit)
fi

mkdir -p data
echo "→ starting cockpit API on :$API_PORT…"
uv run uvicorn accountant.api.server:app --port "$API_PORT" --log-level warning >data/cockpit-api.log 2>&1 &
API_PID=$!
echo "→ starting ingest server on :$INGEST_PORT…"
uv run uvicorn accountant.pipeline.ingest_server:app --host 127.0.0.1 --port "$INGEST_PORT" --log-level warning >data/cockpit-ingest.log 2>&1 &
INGEST_PID=$!
echo "→ starting dashboard (Vite) on :$UI_PORT…"
(cd web && npm run dev >../data/cockpit-ui.log 2>&1) &
UI_PID=$!

cleanup() {
  echo
  echo "→ stopping cockpit…"
  kill "$API_PID" "$INGEST_PID" "$UI_PID" 2>/dev/null || true
  pkill -f "uvicorn accountant.api" 2>/dev/null || true
  pkill -f "ingest_server:app" 2>/dev/null || true
  pkill -f "vite" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

echo "→ waiting for services (the agent reasons on boot, ~6-10s)…"
for _ in $(seq 1 40); do
  if curl -fsS -o /dev/null "http://localhost:$API_PORT/health" 2>/dev/null \
     && curl -fsS -o /dev/null "http://localhost:$UI_PORT" 2>/dev/null; then
    break
  fi
  sleep 1
done

cat <<EOF

  ✓ cockpit API    http://localhost:$API_PORT
  ✓ ingest server  http://localhost:$INGEST_PORT
  ✓ DASHBOARD      http://localhost:$UI_PORT   ← open this in your browser

  stream traffic (optional, separate terminal):
    ACCOUNTANT_INGEST_URL=http://localhost:$INGEST_PORT uv run python -m observed.generate_dataset 100 1

  logs: data/cockpit-{api,ingest,ui}.log
  Ctrl-C to stop all three.

EOF

wait
