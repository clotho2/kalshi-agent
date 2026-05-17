"""Actions taken when the kill switch engages: cancel all resting orders."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.storage.models import Event, Order

log = get_logger(__name__)


class HaltMonitor:
    """Watches the HALT file. On transition disengaged->engaged, cancels all resting orders."""

    def __init__(
        self,
        kill_switch: KillSwitch,
        client: KalshiClient,
        session_maker: sessionmaker,
        discord: DiscordNotifier | None,
    ) -> None:
        self._kill = kill_switch
        self._client = client
        self._sm = session_maker
        self._discord = discord
        self._last_state = kill_switch.is_engaged()

    async def run(self, stop_event: asyncio.Event, tick_seconds: float = 1.0) -> None:
        while not stop_event.is_set():
            state = self._kill.is_engaged()
            if state and not self._last_state:
                await self._on_engaged()
            elif not state and self._last_state:
                self._on_disengaged()
            self._last_state = state
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            except TimeoutError:
                pass

    async def _on_engaged(self) -> None:
        reason = self._kill.reason() or {}
        log.warning("halt_actions_triggered", reason=reason)
        try:
            n = await self._client.cancel_all_resting()
        except Exception as e:
            log.error("cancel_all_failed", error=str(e))
            n = -1
        with self._sm() as s:
            s.add(Event(
                kind="halt_actions",
                level="warning",
                message=f"cancelled {n} resting orders on HALT engage",
                payload={"reason": reason, "cancelled": n},
            ))
            # Mark any local pending/resting orders as cancelled
            orders = s.scalars(
                select(Order).where(Order.status.in_(("pending", "resting", "open")))
            ).all()
            for o in orders:
                o.status = "cancelled_by_halt"
            s.commit()
        if self._discord:
            r = reason.get("reason", "manual") if isinstance(reason, dict) else "manual"
            await self._discord.post(
                f":octagonal_sign: **HALT engaged** — cancelled {n} resting orders. Reason: {r}"
            )

    def _on_disengaged(self) -> None:
        log.warning("halt_disengaged")
        with self._sm() as s:
            s.add(Event(
                kind="halt_actions",
                level="info",
                message="HALT disengaged",
            ))
            s.commit()
