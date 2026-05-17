"""HALT actions: cancel all resting orders on engage."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.safety.halt_actions import HaltMonitor
from kalshi_agent.storage.models import Event, Order


@pytest.mark.asyncio
async def test_engage_cancels_resting_orders(kill_switch, db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Order(
            client_order_id="x", kalshi_order_id="k1",
            market_ticker="FOO", side="yes", action="buy", count=5,
            price_dollars="0.5000", status="resting",
        ))
        s.commit()
    mock_client = MagicMock()
    mock_client.cancel_all_resting = AsyncMock(return_value=3)
    mon = HaltMonitor(kill_switch, mock_client, db_session_maker, discord=None)

    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop, tick_seconds=0.05))
    await asyncio.sleep(0.1)
    kill_switch.engage("test_halt")
    await asyncio.sleep(0.2)
    stop.set()
    await task

    mock_client.cancel_all_resting.assert_awaited()
    with db_session_maker() as s:
        order = s.query(Order).one()
        events = s.query(Event).all()
    assert order.status == "cancelled_by_halt"
    assert any(e.kind == "halt_actions" for e in events)


@pytest.mark.asyncio
async def test_already_engaged_does_not_re_cancel(kill_switch, db_session_maker) -> None:
    kill_switch.engage("pre_existing")
    mock_client = MagicMock()
    mock_client.cancel_all_resting = AsyncMock(return_value=0)
    mon = HaltMonitor(kill_switch, mock_client, db_session_maker, discord=None)

    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop, tick_seconds=0.05))
    await asyncio.sleep(0.15)
    stop.set()
    await task
    # _last_state initialized as True (already engaged), so no transition fires
    mock_client.cancel_all_resting.assert_not_awaited()
