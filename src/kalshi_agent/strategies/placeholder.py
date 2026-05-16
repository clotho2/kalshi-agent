"""Placeholder strategy: emits one signal/hour to prove the pipeline end-to-end."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from kalshi_agent.strategies.base import Signal, Strategy


class PlaceholderStrategy(Strategy):
    name = "placeholder"

    def __init__(self, test_ticker: str, emit_interval_seconds: int = 3600) -> None:
        self._ticker = test_ticker
        self._interval = emit_interval_seconds
        self._last_emit: float | None = None

    async def generate_signals(self) -> list[Signal]:
        now = time.monotonic()
        if self._last_emit is not None and (now - self._last_emit) < self._interval:
            return []
        self._last_emit = now

        return [
            Signal(
                market_ticker=self._ticker,
                side="yes",
                model_probability=0.55,
                confidence=0.1,  # intentionally low — keeps sizing tiny
                rationale="placeholder signal for pipeline validation",
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                strategy_name=self.name,
            )
        ]
