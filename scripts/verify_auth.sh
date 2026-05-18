#!/usr/bin/env bash
# Smoke test: hit Kalshi /portfolio/balance with current creds, verify auth works.
# Use this on the production box before flipping to live mode.
set -euo pipefail
: "${KALSHI_API_KEY_ID:?KALSHI_API_KEY_ID must be set}"
: "${KALSHI_PRIVATE_KEY_PATH:?KALSHI_PRIVATE_KEY_PATH must be set}"
export KALSHI_BASE_URL="${KALSHI_BASE_URL:-https://demo-api.kalshi.co/trade-api/v2}"

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "error: .venv/bin/python not found. Run 'uv sync' or 'python -m venv .venv && .venv/bin/pip install -e .' first." >&2
  exit 1
fi

exec .venv/bin/python - <<'PYEOF'
import asyncio
import os
from pathlib import Path

from kalshi_agent.kalshi.client import KalshiClient


async def main() -> int:
    base = os.environ.get(
        "KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2"
    )
    async with KalshiClient(
        base_url=base,
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_path=Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]),
    ) as c:
        bal = await c.get_balance()
        print(f"OK balance={bal.balance_dollars} base={base}")
    return 0


raise SystemExit(asyncio.run(main()))
PYEOF
