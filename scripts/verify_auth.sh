#!/usr/bin/env bash
# Smoke test: hit Kalshi demo /exchange/status with current creds, verify auth works.
# Use this on the production box before flipping to live mode.
set -euo pipefail
: "${KALSHI_API_KEY_ID:?KALSHI_API_KEY_ID must be set}"
: "${KALSHI_PRIVATE_KEY_PATH:?KALSHI_PRIVATE_KEY_PATH must be set}"
BASE_URL="${KALSHI_BASE_URL:-https://demo-api.kalshi.co/trade-api/v2}"

cd "$(dirname "$0")/.."
exec .venv/bin/python -c "
import asyncio, os, sys
from pathlib import Path
from kalshi_agent.kalshi.client import KalshiClient

async def main():
    base = os.environ['KALSHI_BASE_URL'] if 'KALSHI_BASE_URL' in os.environ else 'https://demo-api.kalshi.co/trade-api/v2'
    async with KalshiClient(
        base_url=base,
        api_key_id=os.environ['KALSHI_API_KEY_ID'],
        private_key_path=Path(os.environ['KALSHI_PRIVATE_KEY_PATH']),
    ) as c:
        bal = await c.get_balance()
        print(f'OK balance={bal.balance_dollars}')

asyncio.run(main())
"
