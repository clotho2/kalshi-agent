#!/usr/bin/env bash
set -euo pipefail
: "${CONTROL_BEARER_TOKEN:?CONTROL_BEARER_TOKEN must be set}"
URL="${KALSHI_AGENT_URL:-http://127.0.0.1:8787}/api/control/reconcile"
curl -fsS -X POST -H "Authorization: Bearer $CONTROL_BEARER_TOKEN" "$URL"
echo
