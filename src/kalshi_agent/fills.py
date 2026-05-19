"""Fill ingestion: polls Kalshi /portfolio/fills, applies them to positions.

Polling is reliable, idempotent, and survives reconnects. WebSocket is a
latency optimization layered on top; polling is the source of truth.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import Config
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_agent.kalshi.types import price_str_to_decimal
from kalshi_agent.positions import apply_fill
from kalshi_agent.storage.models import Event, Fill, Order

UTC = timezone.utc

log = get_logger(__name__)


class FillIngestor:
    def __init__(
        self,
        config: Config,
        client: KalshiClient,
        session_maker: sessionmaker,
        discord: DiscordNotifier | None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._sm = session_maker
        self._discord = discord
        self._high_water_ts: int | None = None
        self._lock = asyncio.Lock()
        self._taker_rate = Decimal(str(config.fees.taker_rate))
        self._maker_ratio = Decimal(str(config.fees.maker_taker_ratio))

    def _load_high_water(self) -> int:
        """Most recent fill ts we've seen, in seconds since epoch."""
        with self._sm() as s:
            latest = s.scalars(
                select(Fill).order_by(Fill.created_at.desc()).limit(1)
            ).first()
        if latest is None:
            return 0
        return int(latest.created_at.timestamp())

    async def catch_up(self) -> int:
        """One-shot catch-up: pull all fills since last known. Returns count."""
        async with self._lock:
            if self._high_water_ts is None:
                self._high_water_ts = self._load_high_water()
            return await self._fetch_and_apply()

    async def _fetch_and_apply(self) -> int:
        applied = 0
        cursor: str | None = None
        min_ts = self._high_water_ts or 0
        max_ts_seen = min_ts
        while True:
            try:
                fills, cursor = await self._client.list_fills(
                    min_ts=min_ts if min_ts > 0 else None,
                    cursor=cursor,
                    limit=200,
                )
            except KalshiAPIError as e:
                log.error("fill_poll_failed", error=str(e))
                break
            if not fills:
                break
            with self._sm() as s:
                for f in fills:
                    realized = apply_fill(
                        s, f,
                        taker_rate=self._taker_rate,
                        maker_taker_ratio=self._maker_ratio,
                    )
                    if realized == 0:
                        continue
                    applied += 1
                    ts = int((f.created_time or datetime.now(UTC)).timestamp())
                    max_ts_seen = max(max_ts_seen, ts)
                    s.add(Event(
                        kind="fill",
                        level="info",
                        message=f"{f.ticker} {f.side.upper()} {f.action} {f.count} "
                                f"realized={realized:.4f}",
                        payload={
                            "ticker": f.ticker, "side": f.side,
                            "count": f.count, "realized": str(realized),
                        },
                    ))
                    if self._discord:
                        await self._discord.post(
                            f":receipt: **{f.ticker}** {f.side.upper()} {f.action} "
                            f"{f.count} @ {price_str_to_decimal(f.yes_price_dollars or f.no_price_dollars or '0')} | "
                            f"realized {realized:+.4f}"
                        )
                    parent = s.scalars(
                        select(Order).where(Order.kalshi_order_id == f.order_id)
                    ).first()
                    if parent is not None and parent.status not in {"filled", "cancelled"}:
                        parent.status = "filled"
                s.commit()
            if not cursor:
                break
        if max_ts_seen > (self._high_water_ts or 0):
            self._high_water_ts = max_ts_seen + 1
        return applied

    async def poll_loop(self, stop_event: asyncio.Event, interval_seconds: int) -> None:
        if self._high_water_ts is None:
            self._high_water_ts = self._load_high_water()
        while not stop_event.is_set():
            try:
                async with self._lock:
                    await self._fetch_and_apply()
            except Exception as e:
                log.error("fill_poll_loop_error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
