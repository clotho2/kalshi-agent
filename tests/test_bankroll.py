"""Bankroll caching and fallback behavior."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.bankroll import Bankroll
from kalshi_agent.kalshi.client import KalshiAPIError
from kalshi_agent.kalshi.types import KalshiBalance


@pytest.mark.asyncio
async def test_fetches_and_caches() -> None:
    mock_client = MagicMock()
    mock_client.get_balance = AsyncMock(return_value=KalshiBalance(balance_dollars="500.0000"))
    b = Bankroll(mock_client, ttl_seconds=10)
    v1 = await b.get()
    v2 = await b.get()
    assert v1 == Decimal("500.0000")
    assert v2 == Decimal("500.0000")
    mock_client.get_balance.assert_awaited_once()  # cache hit on second


@pytest.mark.asyncio
async def test_refresh_on_ttl_expiry() -> None:
    mock_client = MagicMock()
    mock_client.get_balance = AsyncMock(side_effect=[
        KalshiBalance(balance_dollars="500.0000"),
        KalshiBalance(balance_dollars="600.0000"),
    ])
    b = Bankroll(mock_client, ttl_seconds=0.01)
    v1 = await b.get()
    await asyncio.sleep(0.02)
    v2 = await b.get()
    assert v1 == Decimal("500.0000")
    assert v2 == Decimal("600.0000")


@pytest.mark.asyncio
async def test_falls_back_on_api_error_with_no_cache() -> None:
    mock_client = MagicMock()
    mock_client.get_balance = AsyncMock(side_effect=KalshiAPIError(500, "boom"))
    b = Bankroll(mock_client, fallback_dollars=Decimal("42"))
    v = await b.get()
    assert v == Decimal("42")


@pytest.mark.asyncio
async def test_keeps_last_good_on_api_error() -> None:
    mock_client = MagicMock()
    mock_client.get_balance = AsyncMock(side_effect=[
        KalshiBalance(balance_dollars="500.0000"),
        KalshiAPIError(500, "boom"),
    ])
    b = Bankroll(mock_client, ttl_seconds=0.01, fallback_dollars=Decimal("0"))
    v1 = await b.get()
    await asyncio.sleep(0.02)
    v2 = await b.get()
    assert v1 == Decimal("500.0000")
    assert v2 == Decimal("500.0000")  # falls back to last good
