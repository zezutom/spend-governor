#!/bin/sh
# Cloud Run's filesystem is read-only except /tmp, and the control plane writes
# policy state to the DB. Copy the baked, read-only corpus to a writable path on
# each cold start (which also gives every instance a clean demo to start from).
set -e

if [ -f /app/data/accountant.db ]; then
  cp -f /app/data/accountant.db /tmp/accountant.db
fi

# Cloud Run injects $PORT (default 8080). Bind there.
exec uv run --no-sync uvicorn accountant.api.server:app \
  --host 0.0.0.0 --port "${PORT:-8080}" --log-level warning
