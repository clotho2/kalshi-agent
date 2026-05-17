"""Bankroll provider: caches balance from Kalshi for use in Kelly sizing."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_agent.kalshi.types import price_str_to_decimal

log = get_logger(__name__)


class Bankroll:
    """Caches /portfolio/balance for `ttl_seconds`. Falls back to last good value on error."""

    def __init__(self, client: KalshiClient, ttl_seconds: float = 10.0,
                 fallback_dollars: Decimal = Decimal("0")) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._fallback = fallback_dollars
        self._cached: Decimal | None = None
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> Decimal:
        async with self._lock:
            try:
                bal = await self._client.get_balance()
                self._cached = price_str_to_decimal(bal.balance_dollars)
                self._last_fetch = time.monotonic()
            except KalshiAPIError as e:
                log.warning("balance_fetch_failed", error=str(e))
                if self._cached is None:
                    self._cached = self._fallback
            return self._cached

    async def get(self) -> Decimal:
        if self._cached is None or (time.monotonic() - self._last_fetch) > self._ttl:
            return await self.refresh()
        return self._cached
