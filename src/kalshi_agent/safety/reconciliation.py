"""Reconciliation: Kalshi is ground truth, local DB is mirror."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.storage.models import Event, Position

log = get_logger(__name__)


class Reconciler:
    def __init__(
        self,
        client: KalshiClient,
        session_maker: sessionmaker,
        discord: DiscordNotifier | None = None,
    ) -> None:
        self._client = client
        self._sm = session_maker
        self._discord = discord
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def reconcile(self, *, source: str = "scheduled") -> dict:
        async with self._lock:
            return await self._reconcile_inner(source)

    async def _reconcile_inner(self, source: str) -> dict:
        log.info("reconciliation_start", source=source)
        try:
            remote = await self._client.list_positions()
        except Exception as e:
            log.error("reconciliation_fetch_failed", error=str(e))
            return {"ok": False, "error": str(e)}

        remote_by_ticker = {p.ticker: p for p in remote}
        discrepancies: list[dict] = []

        with self._sm() as s:
            local = {p.market_ticker: p for p in s.scalars(select(Position)).all()}

            # Update or insert from remote
            for ticker, rpos in remote_by_ticker.items():
                lpos = local.get(ticker)
                # Map signed Kalshi position to (side, count)
                if rpos.position == 0:
                    side, count = "yes", 0
                else:
                    side = "yes" if rpos.position > 0 else "no"
                    count = abs(rpos.position)
                if lpos is None:
                    s.add(Position(market_ticker=ticker, side=side, count=count,
                                   avg_price_dollars="0.0000"))
                    discrepancies.append({"ticker": ticker, "kind": "missing_local", "remote": count})
                elif lpos.count != count or lpos.side != side:
                    discrepancies.append({
                        "ticker": ticker, "kind": "mismatch",
                        "local": {"side": lpos.side, "count": lpos.count},
                        "remote": {"side": side, "count": count},
                    })
                    lpos.side = side
                    lpos.count = count

            # Local positions absent from remote = closed externally
            for ticker, lpos in local.items():
                if ticker not in remote_by_ticker and lpos.count != 0:
                    discrepancies.append({
                        "ticker": ticker, "kind": "missing_remote",
                        "local": {"side": lpos.side, "count": lpos.count},
                    })
                    lpos.count = 0

            s.add(Event(
                kind="reconciliation",
                level="warning" if discrepancies else "info",
                message=f"reconciled ({source}): {len(discrepancies)} discrepancies",
                payload={"discrepancies": discrepancies, "source": source},
            ))
            s.commit()

        if discrepancies:
            log.warning("reconciliation_discrepancies", count=len(discrepancies),
                        items=discrepancies)
            if self._discord:
                summary = ", ".join(d["ticker"] for d in discrepancies[:5])
                await self._discord.post(
                    f":warning: Reconciliation found {len(discrepancies)} discrepancies: {summary}"
                )
        else:
            log.info("reconciliation_clean", source=source)

        return {"ok": True, "discrepancies": discrepancies}
