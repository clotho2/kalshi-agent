"""Periodic outbound heartbeat to a configured URL (healthchecks.io, etc).

Failure to ping does not affect trading. The endpoint should alert the user
when pings stop arriving.
"""

from __future__ import annotations

import asyncio

import httpx

from kalshi_agent.journal.logger import get_logger

log = get_logger(__name__)


class LivenessHeartbeat:
    def __init__(self, url: str | None, interval_seconds: int = 60) -> None:
        self._url = url
        self._interval = interval_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            log.info("liveness_heartbeat_disabled")
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            while not stop_event.is_set():
                try:
                    await client.get(self._url)
                except Exception as e:
                    log.warning("liveness_heartbeat_failed", error=str(e))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
