#!/usr/bin/env bash
set -euo pipefail
HALT_FILE="${KALSHI_AGENT_HALT_FILE:-/var/lib/kalshi-agent/HALT}"
if [[ -f "$HALT_FILE" ]]; then
  rm -f "$HALT_FILE"
  echo "kalshi-agent HALT cleared"
else
  echo "kalshi-agent was not halted (no file at $HALT_FILE)"
fi
