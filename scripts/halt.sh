#!/usr/bin/env bash
set -euo pipefail
HALT_FILE="${KALSHI_AGENT_HALT_FILE:-/var/lib/kalshi-agent/HALT}"
REASON="${1:-manual_cli_halt}"
mkdir -p "$(dirname "$HALT_FILE")"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$HALT_FILE" <<EOF
{
  "reason": "$REASON",
  "source": "halt.sh",
  "timestamp": "$TS"
}
EOF
echo "kalshi-agent HALT engaged at $HALT_FILE — reason: $REASON"
