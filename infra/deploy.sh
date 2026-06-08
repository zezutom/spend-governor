#!/usr/bin/env bash
# Deploy Agent Accountant as a single Cloud Run service (FastAPI serves the built
# cockpit + /api + the live Phoenix-MCP introspection).
#
# Prereqs: `gcloud auth login`, a GCP project with billing, and the secrets below
# (this script sources .env if present).
#
# Required (env or .env):
#   PHOENIX_API_KEY_OBSERVED_WRITE   Phoenix Cloud API key (read access to traces)
#   PHOENIX_COLLECTOR_ENDPOINT       e.g. https://app.phoenix.arize.com/s/<space>
#   GOOGLE_API_KEY                   Gemini API key
# Optional:
#   PHOENIX_PROJECT_NAME (default agent-accountant)
#   GCP_PROJECT (default: gcloud's current), REGION (default us-central1),
#   SERVICE (default agent-accountant)
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi

REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-agent-accountant}"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PHOENIX_PROJECT_NAME="${PHOENIX_PROJECT_NAME:-agent-accountant}"

: "${PROJECT:?No GCP project — set GCP_PROJECT or run: gcloud config set project <id>}"
: "${PHOENIX_API_KEY_OBSERVED_WRITE:?set it (env or .env)}"
: "${PHOENIX_COLLECTOR_ENDPOINT:?set it (env or .env)}"
: "${GOOGLE_API_KEY:?set it (env or .env)}"

echo "Deploying '$SERVICE' to project '$PROJECT' (region $REGION)…"

# min-instances 1 keeps a warm instance during the judging window (avoids cold
# start + MCP warmup). timeout 3600 = max, so the SSE stream isn't cut. CPU is
# allocated while the SSE request is open, which is when the governor needs it.
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source . \
  --allow-unauthenticated \
  --cpu 2 --memory 2Gi \
  --timeout 3600 \
  --min-instances 1 --max-instances 3 \
  --set-env-vars "^@@^PHOENIX_API_KEY_OBSERVED_WRITE=${PHOENIX_API_KEY_OBSERVED_WRITE}@@PHOENIX_COLLECTOR_ENDPOINT=${PHOENIX_COLLECTOR_ENDPOINT}@@PHOENIX_PROJECT_NAME=${PHOENIX_PROJECT_NAME}@@GOOGLE_API_KEY=${GOOGLE_API_KEY}@@GOOGLE_GENAI_USE_VERTEXAI=false"

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
echo
echo "Deployed: $URL"
echo "Smoke test:  curl -fsS $URL/health"
