"""Strategy ABC + Signal validation + end-to-end mocked execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.kalshi.types import Market, OrderResponse
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.strategies.base import Signal
from kalshi_agent.strategies.placeholder import PlaceholderStrategy


class _FakeBankroll:
    async def get(self) -> Decimal:
        return Decimal("1000")


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


def test_signal_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        Signal(
            market_ticker="FOO", side="yes", model_probability=0.5, confidence=0.5,
            rationale="x", valid_until=datetime.now(),
        )


def test_signal_rejects_out_of_range_probability() -> None:
    with pytest.raises(Exception):
        Signal(
            market_ticker="FOO", side="yes", model_probability=1.5, confidence=0.5,
            rationale="x", valid_until=_future(),
        )


def test_signal_is_expired() -> None:
    s = Signal(
        market_ticker="FOO", side="yes", model_probability=0.6, confidence=0.5,
        rationale="x", valid_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert s.is_expired()


@pytest.mark.asyncio
async def test_placeholder_emits_once_then_rate_limits() -> None:
    s = PlaceholderStrategy("TEST", emit_interval_seconds=3600)
    first = await s.generate_signals()
    second = await s.generate_signals()
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_execution_end_to_end_paper(config, kill_switch, db_session_maker) -> None:
    """Signal -> get_market -> risk check -> create_order -> DB row."""
    from kalshi_agent.execution import Executor

    mock_client = MagicMock()
    mock_client.get_market = AsyncMock(return_value=Market(
        ticker="ECON-TEST", category="economics", status="active",
        yes_ask_dollars="0.5000",
    ))
    mock_client.create_order = AsyncMock(return_value=OrderResponse(
        order_id="srv-1", client_order_id="x", status="resting",
        ticker="ECON-TEST", side="yes", action="buy",
    ))

    risk = RiskMonitor(config, kill_switch, db_session_maker)
    reconciler = Reconciler(mock_client, db_session_maker, discord=None)
    executor = Executor(
        config, mock_client, risk, reconciler, db_session_maker,
        discord=None, bankroll=_FakeBankroll(),
    )

    sig = Signal(
        market_ticker="ECON-TEST", side="yes",
        model_probability=0.70, confidence=1.0,
        rationale="test", valid_until=_future(),
        strategy_name="test_strategy",
    )
    await executor.handle_signal(sig)

    from kalshi_agent.storage.models import Decision, Order
    with db_session_maker() as s:
        decisions = s.query(Decision).all()
        orders = s.query(Order).all()
    assert len(decisions) == 1
    assert decisions[0].accepted
    assert len(orders) == 1
    assert orders[0].kalshi_order_id == "srv-1"
    assert orders[0].status == "resting"


@pytest.mark.asyncio
async def test_execution_rejects_when_killed(config, kill_switch, db_session_maker) -> None:
    from kalshi_agent.execution import Executor

    kill_switch.engage("test")

    mock_client = MagicMock()
    mock_client.get_market = AsyncMock(return_value=Market(
        ticker="ECON-TEST", category="economics", status="active",
        yes_ask_dollars="0.5000",
    ))
    mock_client.create_order = AsyncMock()  # should NOT be called

    risk = RiskMonitor(config, kill_switch, db_session_maker)
    reconciler = Reconciler(mock_client, db_session_maker, discord=None)
    executor = Executor(
        config, mock_client, risk, reconciler, db_session_maker,
        discord=None, bankroll=_FakeBankroll(),
    )

    sig = Signal(
        market_ticker="ECON-TEST", side="yes",
        model_probability=0.70, confidence=1.0,
        rationale="test", valid_until=_future(),
        strategy_name="test_strategy",
    )
    await executor.handle_signal(sig)

    mock_client.create_order.assert_not_called()
    from kalshi_agent.storage.models import Decision
    with db_session_maker() as s:
        d = s.query(Decision).one()
    assert not d.accepted
    assert d.rejection_reason == "kill_switch_engaged"
