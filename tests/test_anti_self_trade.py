"""Anti-self-trade guard: refuse to open new exposure opposite an existing position."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.execution import Executor
from kalshi_agent.kalshi.types import Market, OrderResponse
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.storage.models import Position
from kalshi_agent.strategies.base import Signal


class _FakeBankroll:
    async def get(self) -> Decimal:
        return Decimal("1000")


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_blocks_opposite_side(config, kill_switch, db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Position(
            market_ticker="ECON-TEST", side="yes", count=10,
            avg_price_dollars="0.5000",
        ))
        s.commit()
    mock_client = MagicMock()
    mock_client.get_market = AsyncMock(return_value=Market(
        ticker="ECON-TEST", category="economics", status="active",
        yes_ask_dollars="0.5000", no_ask_dollars="0.5000",
    ))
    mock_client.create_order = AsyncMock()
    risk = RiskMonitor(config, kill_switch, db_session_maker)
    reconciler = Reconciler(mock_client, db_session_maker, discord=None)
    ex = Executor(config, mock_client, risk, reconciler, db_session_maker,
                  discord=None, bankroll=_FakeBankroll())

    sig = Signal(
        market_ticker="ECON-TEST", side="no",
        model_probability=0.7, confidence=1.0,
        rationale="contrary", valid_until=_future(),
        strategy_name="test",
    )
    await ex.handle_signal(sig)
    mock_client.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_allows_same_side_topup(config, kill_switch, db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Position(
            market_ticker="ECON-TEST", side="yes", count=5,
            avg_price_dollars="0.5000",
        ))
        s.commit()
    mock_client = MagicMock()
    mock_client.get_market = AsyncMock(return_value=Market(
        ticker="ECON-TEST", category="economics", status="active",
        yes_ask_dollars="0.5000", no_ask_dollars="0.5000",
    ))
    mock_client.create_order = AsyncMock(return_value=OrderResponse(
        order_id="srv-1", client_order_id="x", status="resting",
        ticker="ECON-TEST", side="yes", action="buy",
    ))
    risk = RiskMonitor(config, kill_switch, db_session_maker)
    reconciler = Reconciler(mock_client, db_session_maker, discord=None)
    ex = Executor(config, mock_client, risk, reconciler, db_session_maker,
                  discord=None, bankroll=_FakeBankroll())

    sig = Signal(
        market_ticker="ECON-TEST", side="yes",
        model_probability=0.7, confidence=1.0,
        rationale="add", valid_until=_future(),
        strategy_name="test",
    )
    await ex.handle_signal(sig)
    mock_client.create_order.assert_awaited()


@pytest.mark.asyncio
async def test_market_not_active_rejected(config, kill_switch, db_session_maker) -> None:
    mock_client = MagicMock()
    mock_client.get_market = AsyncMock(return_value=Market(
        ticker="ECON-TEST", category="economics", status="closed",
        yes_ask_dollars="0.5000",
    ))
    mock_client.create_order = AsyncMock()
    risk = RiskMonitor(config, kill_switch, db_session_maker)
    reconciler = Reconciler(mock_client, db_session_maker, discord=None)
    ex = Executor(config, mock_client, risk, reconciler, db_session_maker,
                  discord=None, bankroll=_FakeBankroll())

    sig = Signal(
        market_ticker="ECON-TEST", side="yes",
        model_probability=0.7, confidence=1.0,
        rationale="x", valid_until=_future(),
        strategy_name="test",
    )
    await ex.handle_signal(sig)
    mock_client.create_order.assert_not_awaited()
