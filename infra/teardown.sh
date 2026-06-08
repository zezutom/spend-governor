#!/usr/bin/env bash
# Tear the public demo down after judging: delete the Cloud Run service AND
# re-enable Domain-Restricted-Sharing on the project (remove the override).
set -euo pipefail

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-agent-accountant}"

echo "Deleting Cloud Run service '$SERVICE'…"
gcloud run services delete "$SERVICE" --region="$REGION" --project="$PROJECT" --quiet || true

echo "Re-enabling Domain-Restricted-Sharing (removing the project override)…"
gcloud resource-manager org-policies delete iam.allowedPolicyMemberDomains \
  --project="$PROJECT" || true

echo "Done. The project is back to the org's default sharing policy and the public URL is gone."
