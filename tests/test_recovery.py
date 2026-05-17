"""Startup recovery: matches in-flight orders to Kalshi server state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.kalshi.types import OrderResponse
from kalshi_agent.recovery import recover_in_flight_orders
from kalshi_agent.storage.models import Order


@pytest.mark.asyncio
async def test_recovers_matching_order(db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Order(
            client_order_id="abc-123",
            kalshi_order_id=None,
            market_ticker="FOO", side="yes", action="buy", count=10,
            price_dollars="0.5000", status="pending",
        ))
        s.commit()
    mock_client = MagicMock()
    mock_client.list_orders = AsyncMock(side_effect=[
        [OrderResponse(order_id="srv-1", client_order_id="abc-123",
                       status="resting", ticker="FOO", side="yes", action="buy")],
        [],
        [],
    ])
    result = await recover_in_flight_orders(mock_client, db_session_maker, discord=None)
    assert result["recovered"] == 1
    assert result["lost"] == 0
    with db_session_maker() as s:
        row = s.query(Order).filter_by(client_order_id="abc-123").one()
    assert row.kalshi_order_id == "srv-1"
    assert row.status == "resting"


@pytest.mark.asyncio
async def test_lost_orders_flagged(db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Order(
            client_order_id="ghost-1",
            kalshi_order_id=None,
            market_ticker="FOO", side="yes", action="buy", count=5,
            price_dollars="0.5000", status="pending",
        ))
        s.commit()
    mock_client = MagicMock()
    mock_client.list_orders = AsyncMock(return_value=[])
    result = await recover_in_flight_orders(mock_client, db_session_maker, discord=None)
    assert result["lost"] == 1
    with db_session_maker() as s:
        row = s.query(Order).filter_by(client_order_id="ghost-1").one()
    assert row.status == "lost"


@pytest.mark.asyncio
async def test_no_pending_orders(db_session_maker) -> None:
    mock_client = MagicMock()
    mock_client.list_orders = AsyncMock(return_value=[])
    result = await recover_in_flight_orders(mock_client, db_session_maker, discord=None)
    assert result == {"pending": 0, "recovered": 0, "lost": 0}
