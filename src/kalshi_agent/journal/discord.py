"""Discord webhook notifier with rate limiting and 429 backoff."""

from __future__ import annotations

import asyncio
import time

import httpx

from kalshi_agent.journal.logger import get_logger

log = get_logger(__name__)


class DiscordNotifier:
    """Posts short messages to a Discord webhook. Best-effort, never raises."""

    def __init__(self, webhook_url: str | None, min_interval: float = 1.0) -> None:
        self._url = webhook_url
        self._min_interval = min_interval
        self._last_post = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def post(self, content: str) -> None:
        if not self.enabled:
            return
        async with self._lock:
            elapsed = time.monotonic() - self._last_post
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(self._url, json={"content": content[:1900]})
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("retry-after", 1.0))
                        await asyncio.sleep(retry_after + 0.5)
                        await client.post(self._url, json={"content": content[:1900]})
            except Exception as e:
                log.warning("discord_post_failed", error=str(e))
            finally:
                self._last_post = time.monotonic()
